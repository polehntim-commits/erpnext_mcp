# SPDX-License-Identifier: MIT
"""The Kairotic Compliance Calendar: what a rule is, and the sweep that runs them.

CHRONOS SERVES KAIROS, AND THIS FILE IS WHERE THAT STOPS BEING A SLOGAN.

The clock runs the sweep. The sweep decides nothing. Every night at whatever hour
the scheduler fires, `refresh_compliance_alerts` walks the operational and
evidence records and asks each rule the same question — *is this condition true
right now* — and the answer is read off the state of the world, never off the
calendar. A rule that fires "on the 15th" would be chronological and therefore
suspect. A rule that fires when the certificate is genuinely inside its renewal
lead time, or when the block being sprayed genuinely has no current water test,
is kairotic and therefore correct.

The distinction has teeth here. It is the difference between:

    "It is the first of the month, so remind somebody about water testing"
        — fires on fallow ground, fires on a field tested last week, and is
          ignored by the third month because most of it is noise.

    "This block was sprayed eleven days ago and its agricultural water has not
     been tested in 118 days"
        — fires on exactly the blocks where FSMA Subpart E is engaged, on
          exactly the days it is engaged, and is worth reading every time.

THE SWEEP IS IDEMPOTENT, AND THAT IS THE WHOLE DESIGN. Each alert's docname is
`alert_key(rule, source_doctype, source_docname)` — derived from the rule and the
record, and from NOTHING that changes daily. So tonight's run finds the row it
wrote last night and updates it. If the key carried the due date or the message,
a certificate ticking from 60 days out to 59 would produce a brand new alert every
morning, each one discarding the snooze somebody set on the last, and the calendar
would fill with duplicates of one fact. That failure is silent and cumulative,
which is why the key is computed in one function and the DocType refuses a
document without one.

AUTO-DISMISSAL IS THE OTHER HALF, AND IT IS THE HALF PEOPLE FORGET. An alert
whose condition has resolved is dismissed by the sweep, with `auto_dismissed`
set and no human reason attached. Nobody should have to remember to switch off a
reminder about something that already happened: the water test was done, the
licence was renewed, the cabin was inspected. If the condition comes BACK — the
renewed certificate approaches its new expiry — the same key is un-dismissed and
raised again, because the alert is a statement about the present, not a task
somebody once closed.

A SNOOZE IS NOT A DISMISSAL. Snoozing sets a date; the alert stays, the condition
stays true, and it reappears on its own when the date passes. It is the honest
way to say "not this week", and it expires without anybody having to clear it.

RULES ARE REGISTERED, NOT LISTED. `rules.py` ends each rule with `register(...)`
and this package imports it at load time, exactly as `packets` and `charts` do.
A rule whose `requires` doctypes are absent from a site is skipped by name — a
site without farm_precision_ag gets no spray rules and is told so, rather than
getting an empty calendar that looks like compliance.

NOTHING HERE RAISES. The sweep runs on somebody's scheduler beside their real
work, and a compliance calendar that took the scheduler down would be worse than
one that missed a night. A rule that throws is caught, reported in the run's
report, and the other rules still run.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import frappe

from .. import training as regimes_vocabulary
from ..compat import doctype_exists, has_field

ALERT_DOCTYPE = "Compliance Alert"

#: The Table MultiSelect on Compliance Alert that carries regime tags.
REGIME_FIELD = "regime"

SEVERITY_INFO = "Info"
SEVERITY_WARNING = "Warning"
SEVERITY_CRITICAL = "Critical"

#: Worst first. The calendar sorts on this, and `severity_min` filters on it.
SEVERITY_ORDER = (SEVERITY_CRITICAL, SEVERITY_WARNING, SEVERITY_INFO)

#: Longest a docname may be, which is what `alert_key` has to fit inside.
#: Frappe's `name` column is varchar(140).
MAX_KEY = 140

#: Most alerts one rule may raise in a single sweep. A rule that wants to raise
#: two thousand is a rule whose condition is wrong — usually a field that is
#: empty on every record rather than stale on a few — and filling the calendar
#: with it would bury the twelve that matter. The cap is reported, never silent.
RULE_CAP = 500

#: v0.107.0. THE SEVERITIES A PHONE IS RUNG FOR, AND THE ONLY ONES. `Critical`
#: means an obligation is already breached or expires today — a lapsed applicator
#: licence, an uninspected cabin, a detector that was never tested. `Warning` and
#: `Info` are the calendar working as designed: a certificate expiring in five
#: weeks is exactly what the calendar is FOR, and it is not worth a notification
#: on a Sunday.
#:
#: This is the whole difference between a feed foremen read and one they turn
#: off. A full sweep on a real operation refreshes dozens of open items every
#: night; pushing all of them would train every supervisor on the farm to swipe
#: erpnext_mcp notifications away without looking, and the first thing lost when
#: that habit sets in is the break horn.
#:
#: ADDING A SEVERITY HERE BREAKS `MAX_PUSHES_PER_SWEEP` BELOW, which is not
#: obvious from this line and is the reason this sentence exists. That cap takes
#: the first ten candidates, which is only safe while every candidate is equally
#: urgent — i.e. while this set holds exactly one severity. Read its comment
#: before widening this.
PUSH_SEVERITIES = frozenset({SEVERITY_CRITICAL})

#: v0.107.0. MOST PHONES-RINGING EVENTS ONE SWEEP WILL PRODUCE. The sibling of
#: `RULE_CAP`, for the same failure in a different place and with the same
#: promise: it is REPORTED, never silent.
#:
#: The case this exists for is the FIRST sweep on an established farm — an
#: operation installs this app in August with four years of camp records, no
#: detector tests on file and thirty expired certificates, and every one of those
#: is legitimately Critical on the night the calendar first runs. Without a cap
#: that is one notification per alert per supervisor, delivered at once, and the
#: foreman's response to a phone that buzzes ninety times is to turn the whole
#: category off. The alerts are all still RAISED — nothing here touches the
#: calendar — and `push_suppressed` says how many did not ring, so the operator
#: reading the report can see the backlog rather than infer it from silence.
#:
#: THIS CAP IS ONLY SAFE BECAUSE `PUSH_SEVERITIES` IS CRITICAL-ONLY, and that
#: coupling is invisible from either constant on its own — which is why it is
#: written here rather than left to be rediscovered. Every candidate for the ten
#: slots is already Critical, so taking the first ten cannot silently outrank the
#: most urgent: there is nothing more urgent among them to outrank. Widen
#: `PUSH_SEVERITIES` to include `Warning` and that stops being true the same day.
#: A sweep would then spend its whole budget on ten certificates expiring in five
#: weeks, drop a cabin nobody may sleep in, and report `push_suppressed: 1` —
#: which reads as a small backlog and is in fact the one alert that mattered.
#:
#: So: anybody adding a severity here has to make the cap severity-aware in the
#: same commit — order the candidates worst-first, or give Critical its own
#: budget. `_push_and_note` is where that would go.
MAX_PUSHES_PER_SWEEP = 10


@dataclass
class Observation:
	"""One true statement about one record, at the moment the sweep looked.

	Deliberately NOT called an alert. An observation is what a rule produces; the
	alert is the durable row the sweep reconciles observations against, and it
	carries things the rule knows nothing about — when the condition was first
	seen, whether somebody snoozed it, why somebody dismissed it.
	"""

	source_doctype: str
	source_docname: str
	message: str
	severity: str = SEVERITY_WARNING
	due_date: str = ""
	company: str = ""
	#: Overrides the rule's category. Used where one rule covers two kinds of
	#: thing — a certificate that is a licence is Workforce, not Certifications.
	category: str = ""
	#: Overrides the rule's `regimes`, per observation. `None` means "the rule's",
	#: which is the ordinary case; an EMPTY LIST is a deliberate "this one answers
	#: to nothing", and the two are different answers.
	#:
	#: Two rules need this and neither could be served by a constant on the rule.
	#: `certification_expiring` fires on certificates of eleven different types and
	#: an applicator licence is WPS evidence while a GlobalGAP certificate is not,
	#: so the regime is a property of the ROW. `training_expiring` copies the tags
	#: off the training record itself, which is the only honest source: the record
	#: says what that afternoon actually covered, and inheriting from the
	#: curriculum would produce an alert claiming a session satisfied a regime it
	#: ran out of time before reaching.
	regimes: list = None
	#: v0.22.0. Anything the engine noticed while computing this observation that
	#: a reader should know: a scope filter skipped because this site has not got
	#: the column, a message template that would not render, an operation the
	#: `custom_python` sandbox refused. NOT AN ERROR — the observation is still
	#: true and the alert is still raised. It rides on the observation because
	#: what it is about is this row, and the sweep report gathers them per rule.
	computation_warnings: list = None


@dataclass(frozen=True)
class Rule:
	"""One condition worth knowing about, and the state that makes it true.

	`scan` takes a context dict — `{"today": ..., "company": ...}` — and returns
	Observations. It reads; it writes nothing. That separation is what lets the
	whole rule set be run in a dry run, and what lets a rule be tested by calling
	it rather than by running a sweep and reading the database afterwards.

	`kairotic_gate` is prose, and it is required. It is the sentence explaining
	what makes this rule fire on ripeness rather than on a date — and a rule whose
	author cannot write that sentence has written a calendar reminder, which
	belongs in somebody's phone rather than in a compliance system.
	"""

	key: str
	title: str
	category: str
	scan: object
	kairotic_gate: str
	purpose: str = ""
	requires: tuple = ()
	#: Framework(s) this rule serves, for the docs and the calendar readout.
	framework: str = ""
	#: Which audits an alert from this rule is evidence for, as canonical regime
	#: tokens. v0.19.2, and it is what lets a calendar be read one audit at a time
	#: — "everything OR-OSHA will ask about in October" is a different afternoon's
	#: work from "everything", and before this the only way to get it was to know
	#: which of twelve rule names belonged to which agency.
	#:
	#: `framework` above is PROSE and this is a KEY, which is why both exist: the
	#: prose names the regulation with its citation for somebody reading the rule,
	#: and the key is what a filter and a packet match on. A rule that answers to
	#: no outside body carries `("Internal",)` rather than nothing, because an
	#: untagged alert is invisible to every regime filter, and silently invisible
	#: is the one thing a compliance calendar must not be.
	regimes: tuple = ("Internal",)

	# ── v0.22.0: where this definition came from ────────────────────────────
	#: The Compliance Rule docname this Rule was assembled from, or "" when it
	#: is one of the definitions shipped in `rules.py` — which is what a site
	#: mid-migrate runs. A reader asking "which row raised this" needs the
	#: answer to be a docname or an honest blank, never a guess.
	record: str = ""
	#: Which version of that record. Carried so a sweep report, and an operator
	#: reading it a month later, can say which definition was in force.
	version: int = 1
	#: `declarative`, `builtin_scanner` or `custom_python`. It is the single most
	#: useful thing to know about a rule after what it does, because it says who
	#: can change it and what changing it costs.
	shape: str = "declarative"
	#: `System`, `Operator` or `AI-proposed`. Provenance, not behaviour: all
	#: three execute the identical deterministic path.
	authored_by: str = ""

	def is_available(self) -> bool:
		try:
			return all(doctype_exists(doctype) for doctype in self.requires)
		except Exception:
			return False

	def missing(self) -> list:
		try:
			return [doctype for doctype in self.requires if not doctype_exists(doctype)]
		except Exception:
			return list(self.requires)

	def describe(self) -> dict:
		return {
			"alert_type": self.key,
			"title": self.title,
			"category": self.category,
			"purpose": self.purpose,
			"kairotic_gate": self.kairotic_gate,
			"framework": self.framework,
			"regimes": list(self.regimes),
			"requires": list(self.requires),
			"available": self.is_available(),
			# v0.22.0, ADDITIVE ONLY. Every key above meant the same thing in
			# v0.21.0 and still does — this app's own iOS build and anybody
			# else's client read that shape, and a rule list that changed shape
			# under them would be a breaking change wearing a feature's clothes.
			"record": self.record or None,
			"version": self.version,
			"shape": self.shape,
			"authored_by": self.authored_by or None,
			"editable": bool(self.record),
		}


#: key → Rule, as SHIPPED. Populated by `register` at import time.
#:
#: v0.22.0 CHANGED WHAT THIS IS FOR. It used to be the rule set the sweep ran.
#: It is now the DEFINITIONS the seeder writes into Compliance Rule records —
#: and the fallback the sweep runs on a site that has the app and has not yet
#: migrated the DocType, so a compliance calendar does not go blank for the
#: length of an upgrade. `rule_map()` is what the sweep actually asks.
RULES: dict = {}


def register(rule: Rule) -> Rule:
	if rule.key in RULES:
		raise RuntimeError(f"duplicate compliance rule {rule.key!r}")
	if not str(rule.kairotic_gate or "").strip():
		raise RuntimeError(
			f"compliance rule {rule.key!r} has no kairotic_gate. Say what state makes it ripe, "
			"or it is a calendar reminder rather than a compliance rule."
		)
	# CHECKED AT IMPORT TIME, LIKE THE GATE ABOVE. A rule tagged 'OSHA' where the
	# vocabulary says 'OR-OSHA' would raise alerts that no OR-OSHA filter and no
	# OR-OSHA packet ever sees — evidence that is present, correct and invisible,
	# which is the worst of the three ways to be wrong. Failing on import means it
	# cannot ship; failing at sweep time would mean it ships and goes quiet.
	unknown = [tag for tag in (rule.regimes or ()) if not regimes_vocabulary.canon(tag)]
	if unknown:
		raise RuntimeError(
			f"compliance rule {rule.key!r} names regime(s) {', '.join(repr(tag) for tag in unknown)}, "
			f"which are not in the vocabulary. {regimes_vocabulary.vocabulary_note()}"
		)
	RULES[rule.key] = rule
	return rule


def rule_map() -> dict:
	"""`{rule_id: Rule}` — THE RULE SET THE SWEEP RUNS, resolved from records.

	v0.22.0. Every caller that used to read `RULES` directly reads this instead,
	and the difference is the whole release: the definitions now come from
	Compliance Rule rows an operator can edit, version and approve, rather than
	from a dict populated at import time by a code release.

	It falls back to `RULES` — the shipped definitions — on a site whose DocType
	is absent or whose rule table is empty, which is the upgrade window and
	nothing else. That fallback is why installing v0.22.0 cannot empty a
	compliance calendar even for the minutes between the app landing and the
	migrate finishing.
	"""
	return resolve_rules()[0]


def resolve_rules() -> tuple:
	"""(rules, notes). `notes` says so when the shipped fallback was used.

	Split from `rule_map` because the SWEEP has somewhere to put the sentence
	and a bare lookup does not. A fallback nobody is told about is a fallback
	that becomes permanent.
	"""
	try:
		from .engine import rule_set

		return rule_set()
	except Exception as exc:  # pragma: no cover - a site whose engine cannot import
		return dict(RULES), [
			f"the rule engine could not read this site's Compliance Rule records "
			f"({type(exc).__name__}: {exc}), so the sweep ran the definitions that ship with "
			"erpnext_mcp. Nothing was lost and nothing was wrongly dismissed; the site's own "
			"edits to those rules were simply not seen."
		]


def names() -> list:
	return sorted(rule_map())


# ── who an alert is about ───────────────────────────────────────────────────
#: Source registers that name a person in FREE TEXT rather than by link, and the
#: column the name is in. A CLOSED TABLE, and short on purpose: every entry is a
#: place this app will match a typed string against the personnel register, which
#: is the one derivation here that can be wrong about a human being.
#:
#: `Certification.holder` is a `Data` field — the certificate register was built
#: to hold licences issued to the OPERATION as well as to people, and an
#: applicator licence and a GlobalGAP certificate cannot share a Link to
#: Employee. So the applicator-licence alert, which is the one this whole
#: mechanism exists for, has to come through here.
NAMED_IN_TEXT = (("Certification", "holder"),)

#: Employee statuses a free-text name may resolve to. A person who has LEFT is
#: still the subject of an alert about their lapsed licence, and excluding them
#: would silently return "nobody" for exactly the rows an auditor asks about.
#: Suspended and Inactive are included for the same reason.
RESOLVABLE_STATUSES = ("Active", "Inactive", "Suspended", "Left")


def subject_employee(source_doctype: str, source_docname: str, company: str = "") -> str:
	"""The Employee one alert is about, or "". Never raises.

	THREE DERIVATIONS, IN DESCENDING ORDER OF HOW MUCH THEY KNOW, and the order
	matters because only the first two are facts:

	  1. The alert points AT an Employee. The docname is the answer.
	  2. The record it points at carries an `employee` Link. An I-9 Form and an
	     Employee Training Record both do, and following a link somebody else drew
	     is not a guess.
	  3. The record names its subject in free text — see `NAMED_IN_TEXT` — and
	     EXACTLY ONE employee at this company bears that name. Two matches is not
	     a subject, it is two people, and this returns "" for it.

	EMPTY IS A REAL ANSWER AND IS THE COMMON ONE. A stale water test, an
	uninspected cabin and an overdue filing are about the OPERATION. The caller
	must be able to tell "this alert is about nobody" from "this alert is about
	somebody I could not work out", and both of those are "" here — which is why
	nothing downstream is allowed to treat a blank as licence to guess. The phone
	guessing from prose is precisely what this field exists to stop.

	Never raises, because both callers are the nightly sweep: one writes the
	column and one sends the alert up the shadow chain, and neither may take the
	sweep down over a doctype a site has not got.
	"""
	try:
		doctype = str(source_doctype or "").strip()
		docname = str(source_docname or "").strip()
		if not doctype or not docname or not doctype_exists("Employee"):
			return ""

		if doctype == "Employee":
			return docname if frappe.db.exists("Employee", docname) else ""

		if not doctype_exists(doctype):
			return ""

		if has_field(doctype, "employee"):
			linked = str(frappe.db.get_value(doctype, docname, "employee") or "").strip()
			if linked:
				return linked

		for named_doctype, column in NAMED_IN_TEXT:
			if doctype != named_doctype or not has_field(doctype, column):
				continue
			return _by_name(str(frappe.db.get_value(doctype, docname, column) or ""), company)
		return ""
	except Exception:  # pragma: no cover - the sweep must survive anything
		return ""


def _by_name(full_name: str, company: str) -> str:
	"""One employee whose name is exactly this, at this company, or "".

	EXACT AND UNIQUE, BOTH REQUIRED. A substring match would make "Ana" the
	subject of an alert about Ana Mendoza, and a first hit off an ambiguous match
	would put one of two people called Juan Garcia on the other one's expired
	licence — which on this field is not a cosmetic error: the app removes the
	subject from the picker so nobody signs off their own gap, and removing the
	wrong person hides the only worker qualified to do the job.

	The company narrows it and is not optional in practice: a name is only ever
	resolved within the entity whose alert this is, so a holder at one farm cannot
	become a subject at another.

	NOT `tools/evidence.py::_resolve_holder`, THOUGH IT READS THE SAME COLUMN.
	That one answers "which register is this name in" for a display line, tries
	Related Party and Family before Employee, and takes the first hit. This one
	answers "may this person be removed from a picker", which is a control, so it
	is Employee-only, company-scoped and refuses an ambiguous match. Collapsing
	the two would make a licence held by a contractor who is also on the payroll
	resolve differently depending on which question was being asked.
	"""
	name = " ".join(str(full_name or "").split())
	if not name:
		return ""
	# The column is free text and sometimes holds the docname itself, which is
	# not a guess at all and does not go through the name rules below.
	if frappe.db.exists("Employee", name):
		return name
	if len(name) < 4 or " " not in name:
		# One word is not a person on a certificate — it is an issuing body, a
		# department, or the farm itself, all of which appear in this column.
		return ""
	filters = {"employee_name": name, "status": ("in", list(RESOLVABLE_STATUSES))}
	if str(company or "").strip():
		filters["company"] = str(company).strip()
	matches = frappe.db.get_all("Employee", filters=filters, pluck="name", limit=2)
	return str(matches[0]) if len(matches or []) == 1 else ""


def alert_key(rule_key: str, source_doctype: str, source_docname: str) -> str:
	"""The deterministic docname for one rule firing on one record.

	Readable where it fits, hashed where it does not — a docname is something a
	human reads in a list view, and `certification_expiring:Certification:
	GlobalGAP 2026` tells them what it is at a glance in a way a bare digest never
	would. The hash is a suffix rather than a replacement so even a truncated key
	keeps its rule name at the front.

	CARRIES NOTHING THAT CHANGES DAILY. Not the due date, not the severity, not
	the message. See the module docstring: a key that moved would make every
	sweep write a duplicate and orphan every snooze.
	"""
	raw = f"{rule_key}:{source_doctype}:{source_docname}"
	if len(raw) <= MAX_KEY:
		return raw
	# sha1 because this is an IDENTITY, not a signature: it only has to be stable
	# and collision-free across one site's alert keys.
	digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
	return f"{raw[: MAX_KEY - len(digest) - 1]}~{digest}"


def severity_at_least(severity: str, minimum: str) -> bool:
	"""Is `severity` as bad as `minimum` or worse?"""
	try:
		return SEVERITY_ORDER.index(severity) <= SEVERITY_ORDER.index(minimum)
	except ValueError:
		return True


def days_until(today: str, target) -> int | None:
	"""Whole days from `today` to `target`. Negative when `target` has passed."""
	if not target:
		return None
	try:
		return int(frappe.utils.date_diff(str(target), today))
	except Exception:
		return None


def days_since(today: str, target) -> int | None:
	"""Whole days from `target` to `today`. Negative when `target` is in the future."""
	value = days_until(today, target)
	return None if value is None else -value


# ── the sweep ───────────────────────────────────────────────────────────────
def refresh_compliance_alerts(
	company: str = "", today: str = "", dry_run: bool = False, regime: str = "", alert_types=None
) -> dict:
	"""Run every available rule and reconcile the Compliance Alert table to it.

	This is the scheduler's entry point and the `refresh_compliance_alerts` tool's
	handler, and it NEVER RAISES. It runs on somebody's scheduler beside their
	real work; a rule that throws is caught, named in the report, and the rest of
	the sweep continues. A compliance calendar that took the site's scheduler down
	would be considerably worse than one that missed a night.

	Reconciliation, per rule, in this order:

	  1. every observation the rule makes becomes an alert — created if new,
	     refreshed if the key is already there, un-dismissed if it had been
	     auto-dismissed and the condition has come back;
	  2. every existing alert of that rule whose key the rule did NOT observe is
	     auto-dismissed, because its condition has resolved.

	Step 2 is scoped to the rule that owns the alert type. A rule skipped because
	its doctypes are absent auto-dismisses nothing, which is the only safe reading
	— "farm_precision_ag was uninstalled this afternoon" is not evidence that
	anybody performed a water test.

	`regime` (v0.19.2) RUNS ONLY THE RULES THAT ANSWER TO ONE AUDIT. It is for the
	morning before an inspection, when the question is "is the OR-OSHA picture
	current" and re-scanning every block's water is a minute nobody has. A rule it
	skips is skipped in the same sense as one whose doctypes are missing: it raises
	nothing AND DISMISSES NOTHING, which is what makes a narrowed sweep safe. A
	filtered run that auto-dismissed the rules it did not run would clear most of
	the calendar and call it progress.

	It matches on `Rule.regimes`, so the two per-row rules — certificates and
	training — run whenever their UNION contains the regime, and then tag each
	alert from the record. A WPS sweep therefore rescans every certificate and
	keeps the applicator licences.

	`alert_types` (v0.64.0) NARROWS TO NAMED RULES, and it is the tightest of the
	three narrowings for the same reason the other two are safe: a rule it skips
	RAISES NOTHING AND DISMISSES NOTHING. It exists because `complete_farm_task`
	now asks the calendar to look again at the moment the world changed, rather
	than leaving the alert that asked for the work standing until the next
	scheduled sweep. A worker who has just walked a cabin, filed two photographs
	and a signature, and watched the alert that sent them there sit there
	unchanged learns that the calendar is decoration.

	IT IS STILL THE SWEEP AND NOT A SHORTCUT. Nothing here dismisses an alert
	because a task was completed; it re-runs the rule that raised it and lets the
	rule's own condition decide, exactly as the nightly run would. That is the
	only honest way an alert goes away — change the world, and let the sweep
	notice — and calling it sooner does not make it a different mechanism. A task
	completed against a condition that is still true leaves the alert exactly
	where it was, which is the outcome that matters most: the work being done and
	the problem being fixed are two different facts.

	An `alert_types` naming nothing this site has is not an error. It reports the
	names as skipped and changes nothing, because a rule that has been renamed or
	disabled since a task was raised is a normal state of a live site and not a
	reason to fail a completion that has already been filed.
	"""
	today = today or frappe.utils.today()
	wanted = regimes_vocabulary.canon(regime) if regime else ""
	named = {str(key).strip() for key in (alert_types or []) if str(key).strip()}
	# ONE RESOLUTION PER SWEEP, and everything below reads this dict. Resolving
	# per rule would mean a rule superseded halfway through a sweep changed
	# definition mid-run, which is exactly the race versioning-by-copy exists to
	# make impossible — so the snapshot is taken here, once, and held.
	rules, engine_notes = resolve_rules()
	rule_names = sorted(rules)
	report = {
		"today": today,
		"company": company or None,
		"regime": wanted or None,
		"alert_types": sorted(named) or None,
		# A name this site does not have is REPORTED rather than raised. A rule
		# renamed or disabled since a task was raised is an ordinary state of a
		# live site, and it must not be able to fail a completion already filed.
		"alert_types_not_on_this_site": sorted(named - set(rule_names)) or None,
		"dry_run": bool(dry_run),
		"created": 0,
		"refreshed": 0,
		"auto_dismissed": 0,
		"reopened": 0,
		# v0.107.0. NONE OF THESE THREE IS DERIVABLE FROM THE FOUR COUNTS ABOVE,
		# which is why they are separate numbers. Only Critical alerts push, so
		# `pushed_alerts` <= `created` + `reopened`; a site with no APNs key
		# raises every alert and rings nothing, so `pushed` can be 0 while
		# `pushed_alerts` is not; and `push_suppressed` is the backlog past
		# `MAX_PUSHES_PER_SWEEP`. See `_note_push` and `_push_and_note`.
		"pushed": 0,
		"pushed_alerts": 0,
		"push_suppressed": 0,
		"rules_run": [],
		"rules_skipped": [],
		"rules_failed": [],
		"alerts": [],
	}
	if engine_notes:
		report["engine_notes"] = list(engine_notes)

	if not doctype_exists(ALERT_DOCTYPE):
		report["rules_skipped"] = [
			{
				"alert_type": key,
				"reason": (
					"this site has no Compliance Alert DocType, which ships with erpnext_mcp — "
					"run `bench --site <site> migrate`"
				),
			}
			for key in rule_names
		]
		return report

	for key in rule_names:
		rule = rules[key]
		if named and key not in named:
			report["rules_skipped"].append(
				{
					"alert_type": key,
					"title": rule.title,
					"reason": (
						"the sweep was narrowed to "
						f"{', '.join(sorted(named))} and this rule is not among them. It raised "
						"nothing AND DISMISSED NOTHING — a narrowed sweep that cleared the rules it "
						"did not run would empty most of the calendar and look like progress."
					),
				}
			)
			continue
		if wanted and wanted not in regimes_vocabulary.parse(rule.regimes):
			report["rules_skipped"].append(
				{
					"alert_type": key,
					"title": rule.title,
					"regimes": list(rule.regimes),
					"reason": (
						f"this rule does not raise {wanted} evidence, and the sweep was narrowed to "
						f"{wanted}. It raised nothing AND DISMISSED NOTHING — the alerts it owns are "
						"exactly as they were, because a narrowed sweep that cleared the rules it "
						"did not run would empty most of the calendar and look like progress. Run "
						"it without `regime` for the whole picture."
					),
				}
			)
			continue
		if not rule.is_available():
			report["rules_skipped"].append(
				{
					"alert_type": key,
					"title": rule.title,
					"reason": (
						f"this site does not have {', '.join(rule.missing())}, so there is nothing "
						"for this rule to read. No alert of this type was created, and none was "
						"dismissed — an absent DocType is not evidence that anybody did the work."
					),
				}
			)
			continue
		try:
			_run_rule(rule, {"today": today, "company": company}, report, dry_run)
		except Exception as exc:
			report["rules_failed"].append(
				{"alert_type": key, "title": rule.title, "error": f"{type(exc).__name__}: {exc}"}
			)

	report["rules_run"] = sorted(
		{entry["alert_type"] for entry in report["alerts"]}
		| {key for key in rule_names if rules[key].is_available()}
		- {entry["alert_type"] for entry in report["rules_failed"]}
		# A rule the `regime` filter skipped did not run, and saying it did would
		# make a narrowed sweep read as a full one in the very report an operator
		# checks to find out whether it was.
		- {entry["alert_type"] for entry in report["rules_skipped"]}
	)
	return report


def _run_rule(rule: Rule, context: dict, report: dict, dry_run: bool) -> None:
	observations = list(rule.scan(context) or [])
	if len(observations) > RULE_CAP:
		report.setdefault("capped", []).append(
			{
				"alert_type": rule.key,
				"observed": len(observations),
				"kept": RULE_CAP,
				"note": (
					f"{rule.key} observed {len(observations)} records, past the {RULE_CAP} cap. A "
					"rule firing on this many records is usually firing on a field that is empty "
					"everywhere rather than stale on a few. The calendar shows the first "
					f"{RULE_CAP}; the rest are neither shown nor dismissed."
				),
			}
		)
		observations = observations[:RULE_CAP]

	# v0.22.0. A rule that scanned unscoped because this site has not got a column,
	# or whose message template would not render, produced TRUE observations that
	# are nonetheless worth a sentence — and a warning nobody surfaces is a
	# warning that is not one. Gathered per rule rather than per observation
	# because the fix is always to the rule.
	warnings = sorted(
		{warning for observation in observations for warning in (observation.computation_warnings or ())}
	)
	if warnings:
		report.setdefault("rules_warned", []).append(
			{"alert_type": rule.key, "title": rule.title, "warnings": warnings}
		)

	observed = {}
	for observation in observations:
		key = alert_key(rule.key, observation.source_doctype, observation.source_docname)
		observed[key] = observation

	existing = _existing_alerts(rule.key, context.get("company") or "")

	for key, observation in sorted(observed.items()):
		row = existing.get(key)
		outcome = _upsert(rule, key, observation, row, context["today"], dry_run, report)
		report[outcome] += 1
		report["alerts"].append(
			{
				"name": key,
				"alert_type": rule.key,
				"outcome": outcome,
				"severity": observation.severity,
				"source": f"{observation.source_doctype} {observation.source_docname}",
				"due_date": observation.due_date or None,
			}
		)

	for key, row in sorted(existing.items()):
		if key in observed:
			continue
		if frappe.utils.cint(row.get("dismissed")):
			continue
		report["auto_dismissed"] += 1
		report["alerts"].append(
			{
				"name": key,
				"alert_type": rule.key,
				"outcome": "auto_dismissed",
				"severity": row.get("severity"),
				"source": f"{row.get('source_doctype')} {row.get('source_docname')}",
				"due_date": str(row.get("due_date") or "") or None,
			}
		)
		if not dry_run:
			_auto_dismiss(key)


def _existing_alerts(alert_type: str, company: str) -> dict:
	"""Every alert of this type already on the site, keyed by docname.

	Includes dismissed ones. A dismissed alert whose condition has come back has
	to be found and re-raised, and a sweep that only looked at live rows would
	create a second alert with the same key and fail on the unique constraint.
	"""
	filters = {"alert_type": alert_type}
	if company:
		filters["company"] = company
	rows = frappe.db.get_all(
		ALERT_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"alert_type",
			"severity",
			"category",
			"company",
			"source_doctype",
			"source_docname",
			"alert_message",
			"due_date",
			"first_seen",
			"snoozed_until",
			"dismissed",
			"auto_dismissed",
			"dismissed_reason",
		],
		limit=RULE_CAP * 4,
	)
	return {str(row["name"]): dict(row) for row in rows or []}


def regimes_of(observation: Observation, rule: Rule) -> list:
	"""What this one alert answers to: the observation's tags, else the rule's.

	`None` on the observation means "whatever the rule says", which is the
	ordinary case. An EMPTY LIST means the rule looked and found nothing — a
	training record carrying no regime tag produces an alert carrying none either,
	and inventing one so the row is not bare would be this app asserting evidence
	the record does not support.
	"""
	source = rule.regimes if observation.regimes is None else observation.regimes
	return regimes_vocabulary.parse(source)


def _write_regimes(doc, observation: Observation, rule: Rule) -> None:
	"""Set the alert's regime rows. Never raises.

	Wrapped because this is the ONE part of an upsert that touches a doctype the
	sweep cannot assume: a site mid-upgrade has `Compliance Alert` and not yet
	`Compliance Regime Link`, and an alert that is raised with no tag is very much
	better than a sweep that stops raising alerts. The tags come back on the next
	run after the migrate, because the sweep refreshes every alert it observes.
	"""
	try:
		regimes_vocabulary.set_rows(doc, REGIME_FIELD, regimes_of(observation, rule))
	except Exception:  # pragma: no cover - a site mid-migration
		pass


def _write_subject(doc, observation: Observation) -> None:
	"""Set the alert's `subject_employee`. Never raises.

	Guarded on the column existing for the same reason `_write_regimes` is
	guarded on its child doctype: a site running this release before
	`bench migrate` still has to raise alerts, and one raised without a subject is
	very much better than a sweep that stops raising them. The column fills on the
	next run after the migrate, because the sweep refreshes every alert it
	observes.
	"""
	try:
		if not has_field(ALERT_DOCTYPE, "subject_employee"):
			return
		doc.subject_employee = (
			subject_employee(observation.source_doctype, observation.source_docname, observation.company)
			or None
		)
	except Exception:  # pragma: no cover - a site mid-migration
		pass


def _note_push(report, answer: dict) -> None:
	"""Put one push's outcome on the sweep report. Never raises.

	`report` is optional so the only caller that has one passes it and any other
	caller — a test, a future single-alert path — does not have to invent one.
	The counts are what an operator reads to answer "did anybody's phone ring
	last night", which is a different question from "was the alert raised" and
	has to be answerable separately: a sweep that raised four Criticals and
	pushed none of them is a farm with no p8 key or no enrolled foreman, and
	both are fixable once somebody can see them.
	"""
	if report is None or not isinstance(answer, dict):
		return
	try:
		report["pushed"] = report.get("pushed", 0) + int(answer.get("sent") or 0)
		reason = str(answer.get("reason") or "")
		if reason and reason not in ("sent", "severity_below_threshold"):
			notes = report.setdefault("push_notes", [])
			if reason not in notes:
				notes.append(reason)
	except Exception:  # pragma: no cover - a report is a plain dict
		pass


def _push_and_note(report, doc, phase: str) -> None:
	"""Ring the supervisors about one raised alert, up to the sweep's cap.

	The cap is checked HERE rather than inside `_push_alert` so that the severity
	gate stays the only thing `_push_alert` decides, and so that a Warning does
	not consume one of the ten — an alert that was never going to ring a phone
	must not push a Critical out of the budget. See `MAX_PUSHES_PER_SWEEP`.

	Never raises: `_push_alert` and `_note_push` each hold that contract, and this
	function only counts.
	"""
	if report is not None and int(report.get("pushed_alerts") or 0) >= MAX_PUSHES_PER_SWEEP:
		if str(getattr(doc, "severity", "") or "") in PUSH_SEVERITIES:
			report["push_suppressed"] = int(report.get("push_suppressed") or 0) + 1
		return

	answer = _push_alert(doc, phase)
	if report is not None and str(answer.get("reason") or "") != "severity_below_threshold":
		report["pushed_alerts"] = int(report.get("pushed_alerts") or 0) + 1
	_note_push(report, answer)


def _upsert(
	rule: Rule, key: str, observation: Observation, row, today: str, dry_run: bool, report=None
) -> str:
	"""Create, refresh or reopen one alert. Returns which of the three it was."""
	category = observation.category or rule.category

	if row is None:
		if not dry_run:
			doc = frappe.new_doc(ALERT_DOCTYPE)
			doc.alert_key = key
			doc.alert_type = rule.key
			doc.severity = observation.severity
			doc.category = category
			doc.company = observation.company or None
			doc.source_doctype = observation.source_doctype
			doc.source_docname = observation.source_docname
			_write_subject(doc, observation)
			doc.alert_message = observation.message
			doc.due_date = observation.due_date or None
			doc.first_seen = today
			doc.last_refreshed = frappe.utils.now()
			_write_regimes(doc, observation, rule)
			doc.insert(ignore_permissions=True)
			# v0.85.0. ONLY ON "created", NEVER ON A REFRESH. The sweep refreshes
			# every open alert every night; a copy per refresh would be one row
			# per alert per night forever, and a feed that grows without bound is
			# a feed nobody opens. Raising an alert is the event; noticing it is
			# still true is not.
			_shadow_alert(doc)
			# v0.107.0, and on the same "created" branch and for the same reason.
			# See `_push_alert` for why it also fires on a reopen and the shadow
			# log does not.
			_push_and_note(report, doc, "created")
		return "created"

	reopening = bool(frappe.utils.cint(row.get("auto_dismissed")))
	if dry_run:
		return "reopened" if reopening else "refreshed"

	doc = frappe.get_doc(ALERT_DOCTYPE, key)
	doc.severity = observation.severity
	doc.category = category
	doc.company = observation.company or None
	doc.alert_message = observation.message
	doc.due_date = observation.due_date or None
	doc.last_refreshed = frappe.utils.now()
	# REWRITTEN ON EVERY REFRESH, for the reason the regime tags below are: it is
	# DERIVED, nothing a person set is stored in it, and a site that migrates the
	# column onto alerts it already has needs them filled without a backfill nobody
	# runs. It also follows the record — a certificate reassigned to another holder
	# is about somebody else from that night on, and an alert still naming the
	# previous holder would remove the wrong person from the picker.
	_write_subject(doc, observation)
	# REWRITTEN ON EVERY REFRESH, not only on create. A rule retagged in a release
	# has to reach the alerts already on the site, and the alternative — tags fixed
	# at the moment an alert was first raised — would leave an operation's oldest
	# and most chronic items permanently outside the filter that was built to find
	# them. It is derived data; nothing a person set is stored here.
	_write_regimes(doc, observation, rule)
	# `first_seen` is deliberately not touched. An alert open four months is
	# evidence of four months, and resetting it would make a chronic problem look
	# new every morning — see the module docstring.
	if reopening:
		# The condition resolved and has come back. A human dismissal is NOT
		# reopened here: somebody looked at this and decided, and the sweep does
		# not get to overrule them by noticing the same thing again.
		doc.dismissed = 0
		doc.auto_dismissed = 0
		doc.dismissed_by = None
		doc.dismissed_on = None
		doc.dismissed_reason = None
	doc.save(ignore_permissions=True)
	if reopening:
		_push_and_note(report, doc, "reopened")
	return "reopened" if reopening else "refreshed"


def _shadow_alert(doc) -> None:
	"""Send a newly raised alert up the chain, frozen. Never raises. v0.85.0.

	IMPORTED INSIDE THE FUNCTION on purpose. `alerts` is imported by `tools`
	(dispatch generates work from alerts), so a module-level `from ..tools import
	shadow_log` here would put a cycle in the import graph for the sake of one
	call — and this package's whole job is to be importable early enough that the
	nightly sweep can run.

	WHO A COMPLIANCE ALERT IS ABOUT, AND WHAT HAPPENS WHEN IT IS NOBODY. An
	expiring I-9 or a lapsed certificate is about a PERSON, and the chain above
	that person is the chain to walk. A stale water test, an uninspected cabin or
	an overdue filing is about the OPERATION — there is no subject, so there is
	no chain, and `shadow_log.propagate` routes it to whoever sits at the top of
	the company at level 3 instead. Both are right; a feed that raised nothing
	for the second kind would leave the whole compliance half of this release
	decorative, and one that invented a level-1 recipient for it would put a
	water test in a foreman's crew feed.

	Never raises, and the reason is sharper here than at the other three call
	sites: this runs inside the NIGHTLY SWEEP, with nobody watching, and an
	exception would take the rest of the sweep's alerts down with it. See
	`hooks.py` on the bar every scheduled job in this app has to clear.
	"""
	try:
		from ..tools import shadow_log

		# THE SAME DERIVATION THE COLUMN HOLDS, and read off the column first: the
		# alert was just written with it, so the chain this event walks and the
		# person the calendar reports cannot come to disagree. `subject_employee`
		# is the fallback for a site whose schema has not caught up.
		subject = str(getattr(doc, "subject_employee", "") or "").strip() or subject_employee(
			str(doc.source_doctype or ""), str(doc.source_docname or ""), str(doc.company or "")
		)

		shadow_log.propagate(
			event_type=shadow_log.EVENT_ALERT_RAISED,
			source_doctype=ALERT_DOCTYPE,
			source_name=doc.name,
			subject_employee=subject,
			company=str(doc.company or ""),
			occurred_at=str(doc.first_seen or "") or frappe.utils.now(),
			summary=(
				f"{doc.severity or 'Warning'}: {doc.alert_message}"
				+ (f" (due {doc.due_date})" if doc.due_date else "")
			),
		)
	except Exception:  # pragma: no cover - the sweep must survive anything
		pass


def _subject_name(doc) -> str:
	"""The subject Employee's display name, for a lock screen. "" when there is none.

	Reads the column `_write_subject` filled rather than re-deriving it — see
	`subject_employee`, which is the one place that answers "who is this alert
	about" and must stay the one place. Most alerts have no subject at all; that
	is the ordinary case and not a gap to fill with a guess.
	"""
	subject = str(getattr(doc, "subject_employee", "") or "").strip()
	if not subject:
		return ""
	try:
		return str(frappe.db.get_value("Employee", subject, "employee_name") or "")
	except Exception:  # pragma: no cover - a site without HRMS
		return ""


def _push_alert(doc, phase: str) -> dict:
	"""Tell the people who can act on this alert that it exists. v0.107.0. Never raises.

	WHO. `push.supervisor_employees` — everybody holding a role in
	`roles.DISPATCH_ROLES` at this alert's company. Deliberately NOT the subject
	employee: an expiring I-9 is a fact about a worker and an obligation of the
	employer, the worker cannot act on it and the foreman can. The subject's NAME
	rides along in the payload so the notification is actionable without opening
	anything; their handset is not addressed.

	WHEN. `created` and `reopened`, never `refreshed`. This is the same rule
	`_shadow_alert` states — raising an alert is the event, noticing it is still
	true is not — with one deliberate difference: the shadow log is created-only
	and this is not. A reopen means the condition RESOLVED and HAS COME BACK,
	which is news by construction and rare by construction: it takes an
	auto-dismissal and a recurrence, so it cannot become the nightly noise the
	created-only rule exists to prevent. A cabin that passed inspection in June
	and has failed again is exactly the thing a foreman wants a phone to say.

	A HUMAN DISMISSAL IS NEVER PUSHED OVER. `_upsert` only reopens an
	`auto_dismissed` row; somebody who looked at an alert and dismissed it does
	not get rung about it again, for the same reason the sweep does not un-dismiss
	it.

	IMPORTED INSIDE THE FUNCTION, like `_shadow_alert` beside it, but for a
	different reason: `services/__init__.py` states that nothing in that package
	is imported at app load, and `alerts` is. The one call is not worth breaking
	that.

	Never raises. This runs inside the nightly sweep with nobody watching, and an
	exception here would take the rest of the sweep's alerts down with it.
	"""
	try:
		severity = str(getattr(doc, "severity", "") or "")
		notify_roles, notify_severities = _notify_routing(str(getattr(doc, "alert_type", "") or ""))
		if severity not in (notify_severities or PUSH_SEVERITIES):
			return {"reason": "severity_below_threshold", "severity": severity, "sent": 0}

		from ..services import push as push_service

		company = str(getattr(doc, "company", "") or "")
		recipients = push_service.supervisor_employees(company, notify_roles or None)
		payload = push_service.alert_payload(
			alert=str(doc.name or ""),
			severity=severity,
			message=str(getattr(doc, "alert_message", "") or ""),
			due_date=str(getattr(doc, "due_date", "") or ""),
			alert_type=str(getattr(doc, "alert_type", "") or ""),
			subject_name=_subject_name(doc),
		)
		report = push_service.send_push_to_employees(
			recipients,
			payload,
			# See `push.PRIORITY_CONSIDERATE`: this is news for the morning, and
			# the sweep that raises it runs while the farm is asleep.
			priority=push_service.PRIORITY_CONSIDERATE,
			# The alert docname, so a Critical that resolves and returns replaces
			# its own notification rather than stacking a season of them.
			collapse_id=str(doc.name or ""),
		)
		report["phase"] = phase
		report["severity"] = severity
		return report
	except Exception:  # pragma: no cover - the sweep must survive anything
		return {"reason": "error", "sent": 0}


def _notify_routing(alert_type: str) -> tuple:
	"""`(notify_roles, notify_severities)` off the live rule's `extra_parameters`. v0.203.0.

	Item 8 of the rodent-bait batch: there is no per-task or per-alert recipient
	field, and adding one would be a column nobody fills. The rule already is the
	place its tunables live, so the audience is one too — `["Farm Manager"]` on
	the bait rules. Absent means the shipped behaviour: every dispatch role, and
	Critical only. Never raises.
	"""
	try:
		from .. import compliance_rules

		name = compliance_rules.resolve(alert_type)
		if not name:
			return [], []
		raw = frappe.db.get_value(compliance_rules.DOCTYPE, name, "extra_parameters_json")
		extra = compliance_rules.as_object(raw, "extra_parameters")
	except Exception:
		return [], []
	names = [str(entry).strip() for entry in extra.get("notify_roles") or [] if str(entry).strip()]
	severities = [str(entry).strip() for entry in extra.get("notify_severities") or [] if str(entry).strip()]
	return names, severities


def regimes_for_alerts(names) -> dict:
	"""`{alert docname: [regime tokens]}` for a batch of alerts, in one query.

	The read side of `_write_regimes`. Every caller that filters a calendar by
	regime goes through this rather than fetching child rows itself, so there is
	one place that knows the alert's tags live on a child table — and one place to
	change if they ever stop.
	"""
	return regimes_vocabulary.rows_for_parents(ALERT_DOCTYPE, names, REGIME_FIELD)


def alert_matches_regime(tags, wanted: str) -> bool:
	"""Does an alert carrying `tags` belong in a `wanted` packet? By TOKEN.

	Substring matching is the bug this cannot be allowed to have: `"GlobalGAP"`
	contains `"GAP"`, and a GAP filter that matched it would put a different
	scheme's findings in front of a USDA auditor. Same rule, same reason, as
	`training.matches`.
	"""
	target = regimes_vocabulary.canon(wanted)
	return bool(target) and target in regimes_vocabulary.parse(tags)


def _auto_dismiss(key: str) -> None:
	"""Dismiss one alert because its source condition resolved."""
	doc = frappe.get_doc(ALERT_DOCTYPE, key)
	doc.dismissed = 1
	doc.auto_dismissed = 1
	doc.dismissed_on = frappe.utils.today()
	doc.dismissed_by = None
	# Must be empty — the controller refuses a reason on an auto-dismissal, so the
	# two kinds of dismissal stay distinguishable six months later.
	doc.dismissed_reason = None
	doc.save(ignore_permissions=True)


def sweep() -> int:
	"""The scheduler's entry point. Never raises, and returns what it did.

	A bare function with no arguments because that is what `scheduler_events`
	calls, and a return value because a job that reports nothing is a job nobody
	can tell has stopped working.
	"""
	try:
		if not doctype_exists(ALERT_DOCTYPE):
			return 0
		report = refresh_compliance_alerts()
		return int(report["created"] + report["refreshed"] + report["reopened"] + report["auto_dismissed"])
	except Exception:
		try:
			frappe.log_error(
				title="erpnext_mcp: the compliance alert sweep failed",
				message=frappe.get_traceback(),
			)
		except Exception:
			pass
		return 0

# SPDX-License-Identifier: MIT
"""Reading the compliance calendar, and the three ways to take something off it.

THE CALENDAR IS A READ, AND IT IS ALWAYS ON. `get_compliance_calendar` writes
nothing and defaults enabled, because the whole value of the engine is that
somebody — or something — can ask "what is due" without first being granted
permission to change anything. A compliance calendar behind a write switch is a
calendar nobody consults.

THE THREE WAYS OFF THE CALENDAR ARE NOT THE SAME THING, and keeping them
distinct is most of the design:

  * **Auto-dismissal** — the sweep noticed the condition resolved. Nobody did
    anything to the alert; somebody did the WORK. No reason is recorded because
    none is needed, and the controller refuses one.
  * **Snooze** — "not this week". The condition is still true, the alert is still
    there, and it comes back on its own when the date passes. Nothing has to be
    remembered or cleared.
  * **Dismissal** — a person looked and decided this does not need doing. The
    reason is mandatory and is the only part of the record nobody can
    reconstruct: the alert itself the sweep can rebuild from scratch, but why
    somebody judged it unnecessary exists nowhere else. It is the answer to the
    auditor's question when the same finding turns up next year.

WHY BULK DISMISSAL DEMANDS A DRY RUN FIRST. Every other bulk operation in this
app that can touch a lot of rows does the same, and here the specific hazard is
that the entire calendar is one filter away. `{"severity": "Critical"}` typed
where `{"alert_type": "filing_response_due"}` was meant does not fail, does not
look wrong, and leaves an operation reading as compliant while nothing has been
fixed. So the first call returns the list and refuses to write; a second call
with `dry_run=false` and the same filter does the work.

THE ALERTS THEMSELVES ARE NEVER DELETED. Dismissed is a flag. A dismissal is
compliance evidence in its own right — somebody looked at this — and deleting it
would destroy the more valuable half of the record.
"""

import frappe

from .. import alerts, compat, compliance_rules, dashboard
from .. import training as regimes_vocabulary
from ..args import as_bool, as_choice, as_date, as_int, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult

ALERT = alerts.ALERT_DOCTYPE

#: Most alerts one call will return. A calendar with more rows than this is not a
#: calendar; `severity_min` and `category` are how a caller narrows it.
CALENDAR_CAP = 500

#: Most alerts one bulk dismissal will touch. Deliberately well below the read
#: cap: a filter that matches two hundred alerts is a filter somebody should look
#: at again before it writes.
BULK_CAP = 200

#: Shortest a dismissal reason may be. The same judgement `update_journal_entry_party`
#: makes: a mandatory field somebody types "ok" into has been satisfied without
#: being answered.
MIN_REASON = 8

_ALERT_FIELDS = (
	"name",
	"alert_type",
	"severity",
	"category",
	"company",
	"source_doctype",
	"source_docname",
	"subject_employee",
	"alert_message",
	"due_date",
	"first_seen",
	"last_refreshed",
	"snoozed_until",
	"can_dismiss",
	"dismissed",
	"auto_dismissed",
	"dismissed_by",
	"dismissed_on",
	"dismissed_reason",
)


def _require() -> None:
	compat.require_doctype(
		ALERT,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _company(args: dict) -> str | None:
	return resolve_company(as_str(args, "company"), required=False)


def validate_regime(args: dict) -> str:
	"""Public name for `_wanted_regime`, so a wrapper can check before it loops.

	`list_compliance_calendar_for_me` calls one calendar per entity inside a
	try/except that turns any exception into a named per-entity failure — right
	for a broken register, wrong for a mistyped argument. Checking here first is
	what makes the difference between "your regime is not one this app knows" and
	three failures with an empty calendar under them.
	"""
	return _wanted_regime(args)


def _wanted_regime(args: dict) -> str:
	"""The `regime` argument in canonical spelling, or "". Refuses a near-miss.

	REFUSED RATHER THAN IGNORED. A filter nobody understood is a filter that
	returns an empty calendar, and an empty compliance calendar reads as a clean
	one — which is the single most expensive way for this tool to be wrong.
	"""
	regime = as_str(args, "regime")
	if not regime:
		return ""
	found = regimes_vocabulary.canon(regime)
	if not found:
		raise ToolError(
			f"regime {regime!r} is not one this app knows. "
			f"{regimes_vocabulary.vocabulary_note()} Filtering on an unrecognised regime would "
			"return an empty calendar, and an empty calendar reads as a clean one."
		)
	return found


def _regimes_of(row: dict, rule, fetched: list | None = None) -> list:
	"""Which audits one alert answers to, from whichever source actually has it.

	Three sources, in descending order of how much they know, because the callers
	of `_describe` arrive with three different shapes of row:

	  1. `fetched` — the batch read of child rows a list view does once for five
	     hundred alerts rather than five hundred times.
	  2. the row itself, when it came from `doc.as_dict()` and therefore carries
	     its own `regime` table. An empty list here is a real answer: this alert
	     is tagged with nothing.
	  3. the RULE's tags, for a site whose `Compliance Regime Link` DocType is not
	     there at all — i.e. one that has not run `bench migrate` since v0.19.2.

	The third is a fallback and NOT a default. Where the child table exists it is
	authoritative, empty rows included: an alert with no tags is an alert tagged
	with nothing, which is exactly what an untagged training record's alert should
	say. Falling back to the rule in that case would be worse than useless on the
	two rules that tag per row, since it would report the whole union — every
	certificate alert claiming to be evidence for nine regimes at once.
	"""
	if fetched is not None:
		return list(fetched)
	own = row.get(alerts.REGIME_FIELD)
	if own is not None:
		return regimes_vocabulary.from_rows(own)
	return list(rule.regimes) if rule else []


def _describe(row: dict, today: str, regimes: list | None = None) -> dict:
	due = str(row.get("due_date") or "") or None
	snoozed = str(row.get("snoozed_until") or "") or None
	remaining = None
	if due:
		try:
			remaining = int(frappe.utils.date_diff(due, today))
		except Exception:  # pragma: no cover - an unparseable stored date
			remaining = None
	rule = alerts.get(str(row.get("alert_type") or ""))
	return {
		"name": row.get("name"),
		"alert_type": row.get("alert_type"),
		"title": rule.title if rule else None,
		"severity": row.get("severity") or "Warning",
		"category": row.get("category") or "Other",
		"regimes": _regimes_of(row, rule, regimes),
		"company": row.get("company") or None,
		"source_doctype": row.get("source_doctype") or None,
		"source_docname": row.get("source_docname") or None,
		# WHO IT IS ABOUT, WHEN IT IS ABOUT A PERSON. Derived by the sweep from the
		# source record — see `alerts/base.py::subject_employee` — and reported here
		# rather than left to be read out of the message, which is what a client
		# with no such field is reduced to doing. `subject_employee_name` rides
		# along because every screen that has one shows a name, and the alternative
		# is a second call per row.
		#
		# `None` IS A REAL ANSWER AND IS THE COMMON ONE. Most alerts are about the
		# operation, not about anybody, and a reader must not fill a blank here by
		# looking for a name in the prose.
		"subject_employee": row.get("subject_employee") or None,
		"subject_employee_name": _employee_name(row.get("subject_employee")),
		"message": row.get("alert_message"),
		"due_date": due,
		"days_until_due": remaining,
		"overdue": bool(remaining is not None and remaining < 0),
		"first_seen": str(row.get("first_seen") or "") or None,
		"open_for_days": _open_for(row.get("first_seen"), today),
		"last_refreshed": str(row.get("last_refreshed") or "") or None,
		"snoozed_until": snoozed,
		"snoozed": bool(snoozed and snoozed >= today),
		"dismissed": bool(frappe.utils.cint(row.get("dismissed"))),
		"auto_dismissed": bool(frappe.utils.cint(row.get("auto_dismissed"))),
		"dismissed_by": row.get("dismissed_by") or None,
		"dismissed_on": str(row.get("dismissed_on") or "") or None,
		"dismissed_reason": row.get("dismissed_reason") or None,
		"framework": rule.framework if rule else None,
		# ── v0.57.0, both additive and both optional ────────────────────────
		# `can_dismiss` DEFAULTS FALSE on a site that has not migrated the column
		# yet, which is the same answer the column's own default gives — so an
		# alert read through an older schema is one nobody may wave off, which is
		# the safe direction for a permission to fail.
		"can_dismiss": bool(frappe.utils.cint(row.get("can_dismiss"))),
		"signature_request": _signature_request(row),
	}


def _employee_name(employee) -> str | None:
	"""The display name of an alert's subject, or None. Never raises.

	One `get_value` per row, and only for the rows that HAVE a subject — most
	alerts are about the operation and pay nothing for this.
	"""
	name = str(employee or "").strip()
	if not name or not compat.doctype_exists("Employee"):
		return None
	try:
		return str(frappe.db.get_value("Employee", name, "employee_name") or "") or None
	except Exception:  # pragma: no cover - a row that vanished between reads
		return None


def _signature_request(row: dict) -> dict | None:
	"""The blank signature box this alert is about, addressed. Never raises.

	`API_CONTRACT.md` §8.1: an alert carrying one is a row a phone can tap
	through to a signature pad instead of a noticeboard entry somebody has to go
	and find the Farm Task for. Nearly every alert answers None — an overdue
	housing inspection is not a missing signature — and a failure to work one out
	is the same answer as not having one, because a calendar that would not draw
	is worse than a row that cannot be tapped.
	"""
	try:
		from . import signatures

		return signatures.request_for_alert(row)
	except Exception:  # pragma: no cover - a site whose form doctypes are absent
		return None


def _comment(name: str, text: str) -> bool:
	"""Leave a Comment on an alert's timeline. Never raises.

	Failing to write a note is not a reason to fail the snooze that has already
	been made.
	"""
	try:
		comment = frappe.new_doc("Comment")
		comment.comment_type = "Comment"
		comment.reference_doctype = ALERT
		comment.reference_name = name
		comment.content = text
		comment.insert(ignore_permissions=True)
		return True
	except Exception:
		return False


def _open_for(first_seen, today: str):
	if not first_seen:
		return None
	try:
		return int(frappe.utils.date_diff(today, str(first_seen)))
	except Exception:  # pragma: no cover
		return None


def _alert_row(name: str) -> dict:
	name = (name or "").strip()
	if not name:
		raise ToolError("alert is required (a Compliance Alert docname). get_compliance_calendar lists them.")
	if not frappe.db.exists(ALERT, name):
		raise ToolError(
			f"no Compliance Alert called {name!r}. Alerts are generated by the nightly sweep and "
			"their names are stable, so one that is not here has been deleted rather than "
			"renamed. get_compliance_calendar lists what is current. Nothing was changed."
		)
	return dict(
		frappe.db.get_value(ALERT, name, compat.existing_fields(ALERT, _ALERT_FIELDS), as_dict=True) or {}
	)


# ── get_compliance_alert ────────────────────────────────────────────────────
def get_compliance_alert(args: dict) -> ToolResult:
	"""One alert, described the same way `get_compliance_calendar` describes it.

	THE SINGLE-ROW TWIN OF THE CALENDAR. Most callers want the calendar; a caller
	who already has a docname — a mobile client re-reading one alert after acting
	on it, a rectification handler reporting what changed — wants one row without
	paying for the whole sweep. Uses the identical `_describe`, so a row read here
	and a row read off the calendar are never able to disagree.
	"""
	_require()
	row = _alert_row(as_str(args, "alert", required=True))
	today = frappe.utils.today()
	stored = compat.doctype_exists(regimes_vocabulary.REGIME_LINK_DOCTYPE)
	tags = alerts.regimes_for_alerts([row["name"]]) if stored else {}
	described = _describe(row, today, tags.get(row["name"], []) if stored else None)
	return ToolResult(data=described, summary=f"{row['name']}: {described['title']}")


# ── get_compliance_calendar ─────────────────────────────────────────────────
def _camp_maintenance(company, today: str, housing: list) -> dict:
	"""Never raises: a calendar that fails over the bait block is no calendar at all."""
	try:
		from .. import rodent_bait

		return rodent_bait.camp_maintenance(company or "", str(today), housing)
	except Exception as exc:  # pragma: no cover
		return {"alerts": housing, "open_bait_tasks": [], "next_bait_checks": [], "error": str(exc)}


def get_compliance_calendar(args: dict) -> ToolResult:
	"""What is due and what is late, worst first, grouped by category."""
	_require()
	company = _company(args)
	today = as_date(args, "as_of") or frappe.utils.today()

	severity_min = as_str(args, "severity_min") or alerts.SEVERITY_INFO
	if severity_min not in alerts.SEVERITY_ORDER:
		raise ToolError(
			f"severity_min must be one of: {', '.join(alerts.SEVERITY_ORDER)}. Got {severity_min!r}."
		)
	days_ahead = as_int(args, "days_ahead")
	include_snoozed = as_bool(args, "include_snoozed", False)
	include_dismissed = as_bool(args, "include_dismissed", False)
	regime = _wanted_regime(args)

	filters = {}
	if company:
		filters["company"] = company
	if not include_dismissed:
		filters["dismissed"] = 0
	alert_type = as_str(args, "alert_type")
	if alert_type:
		filters["alert_type"] = alert_type
	category = as_str(args, "category")
	if category:
		filters["category"] = as_choice(ALERT, "category", category, "category")

	limit = min(as_limit(args), CALENDAR_CAP)
	rows = frappe.db.get_all(
		ALERT,
		filters=filters,
		fields=compat.existing_fields(ALERT, _ALERT_FIELDS),
		order_by="due_date asc",
		# A regime filter runs in Python (see below), so the fetch has to be able
		# to over-read: asking for `limit` rows and then discarding the ones that
		# do not carry the regime would return fewer than the caller asked for and
		# call it the whole answer. Bounded by the cap either way.
		limit=min(limit * 4, CALENDAR_CAP) if regime else limit,
	)
	# ONE batch read of the child rows, not one per alert. The tags are then
	# matched by TOKEN rather than by substring, which is not fussiness: a `LIKE
	# '%GAP%'` would match GlobalGAP and put another scheme's findings in front of
	# a USDA GAP auditor. Same rule as `training.matches`, same reason.
	#
	# WHERE THE CHILD TABLE EXISTS, IT IS AUTHORITATIVE — an alert with no rows is
	# an alert tagged with nothing, and saying so is the point. The rule-level
	# fallback in `_regimes_of` is only for a site that has not migrated, where
	# the column does not exist at all and `[]` would be a claim rather than a
	# reading.
	stored = compat.doctype_exists(regimes_vocabulary.REGIME_LINK_DOCTYPE)
	tags = alerts.regimes_for_alerts([row.get("name") for row in rows or []]) if stored else {}
	described = [
		_describe(
			dict(row),
			today,
			tags.get(str(row.get("name")), []) if stored else None,
		)
		for row in rows or []
	]
	if regime:
		described = [alert for alert in described if regime in alert["regimes"]]
	described = described[:limit]

	horizon = None
	if days_ahead is not None:
		horizon = str(frappe.utils.add_days(frappe.utils.getdate(today), max(0, days_ahead)))

	shown, hidden_snoozed, beyond_horizon = [], 0, 0
	for alert in described:
		if not alerts.severity_at_least(alert["severity"], severity_min):
			continue
		if alert["snoozed"] and not include_snoozed:
			hidden_snoozed += 1
			continue
		if horizon and alert["due_date"] and alert["due_date"] > horizon and not alert["overdue"]:
			# Past the horizon and not already late. An overdue item is never
			# filtered out by a forward-looking window: it was due in the past.
			beyond_horizon += 1
			continue
		shown.append(alert)

	shown.sort(
		key=lambda alert: (
			alerts.SEVERITY_ORDER.index(alert["severity"]),
			alert["due_date"] or "9999-12-31",
			alert["name"],
		)
	)

	by_category: dict = {}
	for alert in shown:
		by_category.setdefault(alert["category"], []).append(alert)

	counts = {severity: 0 for severity in alerts.SEVERITY_ORDER}
	for alert in shown:
		counts[alert["severity"]] = counts.get(alert["severity"], 0) + 1
	overdue = [alert for alert in shown if alert["overdue"]]

	readout = []
	for severity in alerts.SEVERITY_ORDER:
		group = [alert for alert in shown if alert["severity"] == severity]
		if group:
			readout.append(f"{len(group)} {severity}")
	headline = ", ".join(readout) or "nothing due"

	skipped = [rule.describe() for rule in alerts.rule_map().values() if not rule.is_available()]

	by_regime: dict = {}
	for alert in shown:
		for tag in alert["regimes"] or ["(untagged)"]:
			by_regime[tag] = by_regime.get(tag, 0) + 1

	data = {
		"company": company,
		"as_of": today,
		"severity_min": severity_min,
		"days_ahead": days_ahead,
		"horizon": horizon,
		"regime": regime or None,
		"by_regime": dict(sorted(by_regime.items())),
		"alert_count": len(shown),
		"by_severity": counts,
		"overdue_count": len(overdue),
		"overdue": [alert["name"] for alert in overdue],
		"by_category": {
			key: {
				"count": len(group),
				"worst": min(
					(alert["severity"] for alert in group),
					key=lambda severity: alerts.SEVERITY_ORDER.index(severity),
				),
				"alerts": group,
			}
			for key, group in sorted(
				by_category.items(),
				key=lambda item: min(alerts.SEVERITY_ORDER.index(alert["severity"]) for alert in item[1]),
			)
		},
		# v0.203.0. Camp and housing maintenance in one place — the Housing
		# alerts, the open rodent bait work and each baited place's next check
		# (docs/design/rodent_bait_program.md §11).
		"camp_maintenance": _camp_maintenance(company, today, by_category.get("Housing") or []),
		"hidden_snoozed": hidden_snoozed,
		"hidden_beyond_horizon": beyond_horizon,
		"rules_unavailable_here": skipped,
		"note": (
			"Grouped by category because the categories are chosen so a whole group can be "
			"cleared in one afternoon — every housing item is one walk round the camp, every "
			"certificate is one trip to an agency website. Sorted Critical first inside each. "
			+ (
				f"{hidden_snoozed} snoozed alert(s) are hidden; pass include_snoozed=true. "
				if hidden_snoozed
				else ""
			)
			+ (
				f"{len(skipped)} rule(s) cannot run on this site at all — see "
				"`rules_unavailable_here`. An empty category is not the same as a clean one."
				if skipped
				else ""
			)
		).strip(),
	}
	if regime:
		data["regime_note"] = (
			f"Narrowed to {regime}: only alerts this app has tagged as {regime} evidence are "
			"here, and the counts above are of THAT subset rather than of the whole calendar. "
			"Matching is by tag, never by substring — 'GlobalGAP' contains 'GAP', and a "
			"substring match would put another scheme's findings in front of a USDA GAP "
			"auditor. Drop `regime` for everything the operation owes anybody."
		)
	if not regime and by_regime.get("(untagged)"):
		data["untagged_note"] = (
			f"{by_regime['(untagged)']} alert(s) carry no regime tag and will therefore appear "
			"in NO regime-filtered calendar and no audit packet. On a site that has just "
			"upgraded this is expected until the next sweep runs — refresh_compliance_alerts "
			"fixes it now. Otherwise it is a training record filed without regimes, which the "
			"alert's own message names."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{len(shown)} alert(s)"
			+ (f" tagged {regime}" if regime else "")
			+ f" as of {today}: {headline}, {len(overdue)} overdue"
		),
	)


# ── list_compliance_rules ───────────────────────────────────────────────────
def list_compliance_rules(args: dict) -> ToolResult:
	"""Every rule, with the state that makes it fire rather than the date.

	v0.22.0 RETROFIT, AND THE RETURN SHAPE IS UNCHANGED. Every key this returned
	in v0.21.0 — `rule_count`, `available_here`, `by_category`, `rules`,
	`unavailable_here`, `note`, and on each rule `alert_type`, `title`,
	`category`, `purpose`, `kairotic_gate`, `framework`, `regimes`, `requires`,
	`available` — means exactly what it always meant. This app's own iOS build
	reads that shape, and so does anybody else's client; a list that changed
	shape under them would be a breaking change wearing a feature's clothes.
	What is new is additive: the filters, and the provenance fields that say
	which record a rule lives on, which version it is, what shape it is and
	whether it can be edited.
	"""
	from . import rules as rule_tools

	if compat.doctype_exists(compliance_rules.DOCTYPE) and compliance_rules.rule_rows(
		include_inactive=True, limit=1
	):
		listing = rule_tools.rule_list(args)
		described, filtered = listing["rules"], listing["filtered"]
		editable = True
	else:
		# The upgrade window: the app is here and the DocType is not yet. The
		# shipped definitions are what the sweep is running, so they are what
		# this reports — saying so, rather than answering with an empty register
		# and letting somebody conclude the site has no compliance rules.
		described = _filtered_shipped(args)
		filtered = described != alerts.describe()
		editable = False

	available = [rule for rule in described if rule["available"]]
	unavailable = [rule for rule in described if not rule["available"]]
	by_category: dict = {}
	by_shape: dict = {}
	for rule in described:
		by_category[rule["category"]] = by_category.get(rule["category"], 0) + 1
		by_shape[rule.get("shape") or "declarative"] = by_shape.get(rule.get("shape") or "declarative", 0) + 1

	data = {
		"rule_count": len(described),
		"available_here": len(available),
		"by_category": dict(sorted(by_category.items())),
		"rules": described,
		"unavailable_here": unavailable,
		"note": (
			"Every rule carries a `kairotic_gate`: the state that makes it ripe. None of "
			"them fires on a date alone — a rule that did would fire on fallow ground and on "
			"ground tested last week, and would be ignored by the third month. A rule listed "
			"under `unavailable_here` is missing a DocType, and raises nothing AND dismisses "
			"nothing: an absent DocType is not evidence that anybody did the work."
		),
		# v0.22.0 additions.
		"by_shape": dict(sorted(by_shape.items())),
		"editable": editable,
		"filtered": filtered,
	}
	if editable:
		data["editable_note"] = (
			"These rules are RECORDS on this site. A threshold, a citation, a scope or a message "
			"is changed with update_compliance_rule — which supersedes rather than overwrites, so "
			"the definition an alert was raised under stays readable — and nothing here needs a "
			"code release. `by_shape` says how much of each rule is data: a `declarative` rule is "
			"entirely on its record; a `builtin_scanner` rule keeps every tunable on the record "
			"and delegates the shape of its join to reviewed, shipped code; a `custom_python` rule "
			"is a program in a sandbox, and there are none of those shipped."
		)
	else:
		data["editable_note"] = (
			"This site has no Compliance Rule DocType yet, so these are the definitions that ship "
			"with erpnext_mcp and the sweep is running them. Behaviour is identical either way — "
			"the shipped definitions ARE what gets seeded. Run `bench --site <site> migrate` and "
			"they become records you can edit."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{len(described)} compliance rule(s), {len(available)} runnable on this site"
			+ (" (filtered)" if filtered else "")
		),
	)


def _filtered_shipped(args: dict) -> list:
	"""The shipped rule set, with whichever filters can be answered without records."""
	described = alerts.describe()
	category = as_str(args, "category")
	if category:
		described = [rule for rule in described if str(rule["category"] or "") == category]
	regime = _wanted_regime(args)
	if regime:
		described = [rule for rule in described if regimes_vocabulary.matches(rule["regimes"], regime)]
	shape = as_str(args, "shape")
	if shape:
		described = [rule for rule in described if rule.get("shape") == shape]
	return described


# ── refresh_compliance_alerts ───────────────────────────────────────────────
def refresh_compliance_alerts(args: dict) -> ToolResult:
	"""Run the whole rule set now instead of waiting for tonight.

	MUTATES ONLY THIS APP'S OWN ALERT TABLE. It creates, refreshes and
	auto-dismisses Compliance Alerts and touches no operational record, no
	certificate, no ledger — every rule is a read. That is why it is safe to run
	at any moment, and why the same function is what the nightly scheduler calls.
	"""
	_require()
	company = _company(args)
	today = as_date(args, "as_of") or frappe.utils.today()
	dry_run = as_bool(args, "dry_run", False)
	regime = _wanted_regime(args)

	report = alerts.refresh_compliance_alerts(
		company=company or "", today=today, dry_run=dry_run, regime=regime
	)
	changed = report["created"] + report["refreshed"] + report["reopened"] + report["auto_dismissed"]

	data = {
		**report,
		"note": (
			"Every rule is a READ over operational and evidence records; the only rows "
			"written are this app's own Compliance Alerts. Nothing was created twice: an "
			"alert's docname is derived from its rule and its source record, so tonight's "
			"sweep finds and refreshes what last night's wrote. `auto_dismissed` is the "
			"count of conditions that RESOLVED — the water test was done, the licence was "
			"renewed — and nobody had to switch those off. `reopened` is the count that came "
			"back after resolving; a dismissal a PERSON made is never reopened by the sweep."
		),
	}
	if regime:
		data["regime_note"] = (
			f"NARROWED TO {regime}: only the rules that raise {regime} evidence ran. Every other "
			"rule raised nothing AND DISMISSED NOTHING — its alerts are exactly as they were, "
			"because a filtered sweep that cleared the rules it did not run would empty most of "
			"the calendar and look like progress. `rules_skipped` names each one. The counts "
			f"above are therefore about the {regime} picture only; run it without `regime` before "
			"treating them as the state of the whole operation."
		)
	return ToolResult(
		data=data,
		summary=(
			("dry run: would touch " if dry_run else "")
			+ f"{changed} alert(s)"
			+ (f" across the {regime} rules" if regime else "")
			+ f" — {report['created']} new, {report['refreshed']} refreshed, "
			f"{report['reopened']} reopened, {report['auto_dismissed']} auto-dismissed"
		),
		docstatus_delta="" if dry_run else "0 → 0 (alerts refreshed)",
	)


# ── snooze_alert ────────────────────────────────────────────────────────────
def snooze_alert(args: dict) -> ToolResult:
	"""Hide one alert until a date. Not a dismissal — it comes back on its own."""
	_require()
	row = _alert_row(as_str(args, "alert", required=True))
	until = as_date(args, "until_date", required=True)
	today = frappe.utils.today()
	reason = as_str(args, "reason")

	if frappe.utils.cint(row.get("dismissed")):
		raise ToolError(
			f"{row['name']} is already dismissed, so there is nothing to hide. If the condition "
			"has come back, the next sweep re-raises it — an auto-dismissed alert reopens by "
			"itself. Nothing was changed."
		)
	if until <= today:
		raise ToolError(
			f"until_date {until} is not in the future. A snooze that has already expired is not "
			"a snooze; the alert would be back on the calendar the moment it was read. Nothing "
			"was changed."
		)

	doc = frappe.get_doc(ALERT, row["name"])
	previous = str(doc.snoozed_until or "") or None
	doc.snoozed_until = until
	doc.save(ignore_permissions=True)
	# The reason goes on the timeline rather than into a field. A snooze reason is
	# not the same kind of fact as a dismissal reason — it is "why not this week",
	# which is worth having and is not evidence — and giving it a column would
	# invite it to be read as one.
	if reason:
		_comment(row["name"], f"Snoozed until {until}. {reason}")

	days = int(frappe.utils.date_diff(until, today))
	return ToolResult(
		data={
			**_describe(dict(doc.as_dict()), today),
			"changed": True,
			"previously_snoozed_until": previous,
			"snoozed_for_days": days,
			"reason": reason or None,
			"note": (
				f"The condition is still true and the alert still exists — it is hidden from the "
				f"calendar until {until} and reappears on its own, without anybody having to "
				"clear a flag. If the underlying thing is actually done, the next sweep "
				"auto-dismisses it and the snooze becomes irrelevant. If it genuinely does not "
				"need doing, dismiss_alert is the honest answer and it records why."
			),
		},
		summary=f"snoozed {row['name']} until {until} ({days} day(s))" + (f" — {reason}" if reason else ""),
		docstatus_delta="0 → 0 (updated)",
	)


# ── dismiss_alert ───────────────────────────────────────────────────────────
def _dismissal_reason(args: dict) -> str:
	"""The mandatory reason, or the refusal that says why it is mandatory."""
	reason = as_str(args, "reason", required=True)
	if len(reason) < MIN_REASON:
		raise ToolError(
			"reason must be a real explanation of why this does not need doing. It is the only "
			"part of this record nobody can reconstruct — the alert itself the sweep can rebuild "
			"from the source record, but the judgement cannot be rebuilt from anything. It is "
			"also the answer when the same finding turns up next year. Nothing was changed."
		)
	return reason


def _refuse_if_dismissed(row: dict) -> None:
	if frappe.utils.cint(row.get("dismissed")):
		how = (
			"by the sweep, because its source condition resolved"
			if frappe.utils.cint(row.get("auto_dismissed"))
			else f"by {row.get('dismissed_by') or 'somebody'} on {row.get('dismissed_on')}"
		)
		raise ToolError(f"{row['name']} was already dismissed {how}. Nothing was changed.")


def _record_dismissal(name: str, reason: str, today: str):
	"""Write the judgement onto the alert. ONE PLACE, because it is the evidence.

	Both dismissal routes land here — the operator's at their desk and the gated
	one a handset reaches — so who dismissed, when, and why are recorded the same
	way whichever door was used. An auditor asking "who decided this did not need
	doing" gets one answer with one shape, and a second copy of this write is a
	second shape for the same fact to be recorded in.
	"""
	doc = frappe.get_doc(ALERT, name)
	doc.dismissed = 1
	doc.auto_dismissed = 0
	doc.dismissed_by = str(frappe.session.user)
	doc.dismissed_on = today
	doc.dismissed_reason = reason
	doc.save(ignore_permissions=True)
	return doc


def dismiss_alert(args: dict) -> ToolResult:
	"""Take one alert off the calendar, with a mandatory reason."""
	_require()
	row = _alert_row(as_str(args, "alert", required=True))
	reason = _dismissal_reason(args)
	today = frappe.utils.today()

	_refuse_if_dismissed(row)
	doc = _record_dismissal(row["name"], reason, today)

	return ToolResult(
		data={
			**_describe(dict(doc.as_dict()), today),
			"changed": True,
			"note": (
				"The alert is off the calendar and is NOT deleted. The record that somebody "
				"looked at this and decided it did not need doing is itself compliance evidence, "
				"and it is what an auditor asks for when the same finding turns up twice. "
				"A human dismissal is never reopened by the sweep — if the condition is still "
				"true tomorrow night, the judgement recorded here stands."
			),
			"warning": (
				"The underlying condition has NOT changed. Dismissing an alert about an expired "
				"certificate does not renew the certificate, and the source record still says "
				"what it said."
			),
		},
		summary=f"dismissed {row['name']} — {reason}",
		docstatus_delta="0 → 0 (updated)",
	)


# ── dismiss_compliance_alert ────────────────────────────────────────────────
def dismiss_compliance_alert(args: dict) -> ToolResult:
	"""Dismiss one alert THE ALERT ITSELF SAYS MAY BE DISMISSED. v0.57.0.

	`API_CONTRACT.md` §8.2, and the whole of what it adds over `dismiss_alert` is
	the gate: this refuses an alert whose `can_dismiss` is not set, and that
	default is false.

	WHY THERE ARE TWO ROUTES TO ONE VERB. `dismiss_alert` is somebody at a desk
	with the source record open in the next tab; the judgement and the context
	are in the same place. This one is for the callers who are NOT there — a
	handset in an orchard, a model reading a calendar — and for those the
	question "may this be waved off" was answered before they arrived or it was
	not answered at all. An overdue housing inspection is not a notification:
	dismissing one from a phone leaves a cabin uninspected and the calendar quiet
	about it, which is exactly why the mobile surface shipped with no dismiss at
	all. Some alerts genuinely are stale — one raised against a lease terminated
	in May, a duplicate of a filing already made elsewhere — and somebody with
	the whole picture says so per alert, in advance, by ticking the box.

	THE GATE IS CHECKED HERE AND NOT ONLY ON THE PHONE. The app hides its Dismiss
	button on `can_dismiss=false` and its own contract says that is UI courtesy
	rather than the boundary; a refusal that only exists in a client is not one.

	THE REASON IS THE ENTIRE AUDIT TRAIL for an obligation nobody discharged, so
	it is required, it has to be a sentence, and it is written to the alert by
	the same code the desk-side route uses.
	"""
	_require()
	row = _alert_row(as_str(args, "alert", required=True))
	reason = _dismissal_reason(args)
	today = frappe.utils.today()

	_refuse_if_dismissed(row)
	if not frappe.utils.cint(row.get("can_dismiss")):
		raise ToolError(
			f"{row['name']} is not marked as dismissible, so it cannot be waved off from here. "
			"That default is deliberate: an alert is a statement that something is not compliant, "
			"and closing one without the work being done leaves the condition true and the "
			"calendar quiet about it. Whoever can see the whole picture marks the individual "
			"alerts that genuinely are stale — a finding against a terminated lease, a duplicate "
			"of a filing already made — by ticking 'May Be Dismissed From The Field' on the "
			"Compliance Alert. At a desk, with the source record to hand, dismiss_alert is the "
			"route that does not ask. Nothing was changed."
		)

	doc = _record_dismissal(row["name"], reason, today)
	return ToolResult(
		data={
			**_describe(dict(doc.as_dict()), today),
			"changed": True,
			"note": (
				"The alert is off the calendar and is NOT deleted — the record that somebody "
				"looked at this and decided it did not need doing is itself compliance evidence. "
				"It was dismissible because somebody marked this alert dismissible, and that "
				"marking plus the reason recorded here is the whole account of how an unmet "
				"obligation came to be closed."
			),
			"warning": (
				"The underlying condition has NOT changed and the source record still says what "
				"it said. If the sweep can still see the condition tomorrow night, it stays true "
				"— a dismissal a person made is never reopened, which is why it is recorded "
				"rather than merely obeyed."
			),
		},
		summary=f"dismissed {row['name']} — {reason}",
		docstatus_delta="0 → 0 (updated)",
	)


# ── dismiss_alert_bulk ──────────────────────────────────────────────────────
def dismiss_alert_bulk(args: dict) -> ToolResult:
	"""Dismiss every alert matching a filter. REQUIRES a dry run first.

	The dry run is not politeness. The whole calendar is one filter away: a
	`severity` typed where an `alert_type` was meant matches everything, fails
	nothing, looks fine, and leaves an operation reading as compliant while
	nothing has been fixed. So the first call returns exactly what would be
	dismissed and writes nothing.
	"""
	_require()
	company = _company(args)
	today = frappe.utils.today()
	reason = as_str(args, "reason", required=True)
	if len(reason) < MIN_REASON:
		raise ToolError(
			"reason must be a real explanation, and it is written onto every alert this touches. "
			"Nothing was changed."
		)

	filters = {"dismissed": 0}
	if company:
		filters["company"] = company
	given = 0
	alert_type = as_str(args, "alert_type")
	if alert_type:
		if alert_type not in alerts.names():
			raise ToolError(
				f"no compliance rule called {alert_type!r}. This site knows: "
				f"{', '.join(alerts.names())}. Nothing was changed."
			)
		filters["alert_type"] = alert_type
		given += 1
	category = as_str(args, "category")
	if category:
		filters["category"] = as_choice(ALERT, "category", category, "category")
		given += 1
	severity = as_str(args, "severity")
	if severity:
		if severity not in alerts.SEVERITY_ORDER:
			raise ToolError(
				f"severity must be one of: {', '.join(alerts.SEVERITY_ORDER)}. Nothing was changed."
			)
		filters["severity"] = severity
		given += 1
	source_doctype = as_str(args, "source_doctype")
	if source_doctype:
		filters["source_doctype"] = source_doctype
		given += 1

	if not given:
		raise ToolError(
			"at least one of alert_type, category, severity or source_doctype is required. A "
			"bulk dismissal with no filter is 'dismiss the entire compliance calendar', which "
			"is never what anybody means and would leave this operation reading as compliant "
			"while nothing had been fixed. Nothing was changed."
		)

	rows = frappe.db.get_all(
		ALERT,
		filters=filters,
		fields=compat.existing_fields(ALERT, _ALERT_FIELDS),
		order_by="severity asc, due_date asc",
		limit=BULK_CAP + 1,
	)
	matched = [_describe(dict(row), today) for row in rows]
	over_cap = len(matched) > BULK_CAP
	matched = matched[:BULK_CAP]

	counts = {severity_name: 0 for severity_name in alerts.SEVERITY_ORDER}
	for alert in matched:
		counts[alert["severity"]] = counts.get(alert["severity"], 0) + 1

	plan = {
		"filters": {key: value for key, value in filters.items() if key != "dismissed"},
		"matched": len(matched),
		"by_severity": counts,
		"alerts": [
			{
				"name": alert["name"],
				"alert_type": alert["alert_type"],
				"severity": alert["severity"],
				"source": f"{alert['source_doctype']} {alert['source_docname']}",
				"due_date": alert["due_date"],
				"message": alert["message"],
			}
			for alert in matched
		],
		"reason": reason,
	}
	if over_cap:
		plan["capped"] = (
			f"more than {BULK_CAP} alerts match. Only the first {BULK_CAP} are listed and only "
			"those would be dismissed. Narrow the filter — a filter matching this many is a "
			"filter worth reading again."
		)

	dry_run = as_bool(args, "dry_run", True)
	if dry_run:
		return ToolResult(
			data={
				**plan,
				"dry_run": True,
				"dismissed": 0,
				"note": (
					"NOTHING WAS WRITTEN. Read the list above, then call again with dry_run=false "
					"and the same filter. The dry run is required because the whole calendar is "
					"one filter away: a severity typed where an alert_type was meant matches "
					"everything, fails nothing, and looks exactly like success."
					+ (
						f" {counts.get('Critical', 0)} of these are CRITICAL — something has "
						"stopped being lawful, and dismissing it does not make it lawful again."
						if counts.get("Critical")
						else ""
					)
				),
			},
			summary=f"dry run: would dismiss {len(matched)} alert(s) — {reason}",
		)

	if not matched:
		raise ToolError("no alert matches that filter, so there is nothing to dismiss. Nothing was changed.")

	dismissed, failed = [], []
	for alert in matched:
		try:
			doc = frappe.get_doc(ALERT, alert["name"])
			doc.dismissed = 1
			doc.auto_dismissed = 0
			doc.dismissed_by = str(frappe.session.user)
			doc.dismissed_on = today
			doc.dismissed_reason = reason
			doc.save(ignore_permissions=True)
			dismissed.append(alert["name"])
		except Exception as exc:
			failed.append({"alert": alert["name"], "error": f"{type(exc).__name__}: {exc}"})

	return ToolResult(
		data={
			**plan,
			"dry_run": False,
			"dismissed": len(dismissed),
			"dismissed_alerts": dismissed,
			"failed": failed,
			"note": (
				f"{len(dismissed)} alert(s) are off the calendar, each carrying the same reason. "
				"NONE of the underlying conditions changed: every source record still says what "
				"it said, and every certificate that had expired is still expired. The sweep "
				"will not reopen these — a dismissal a person made stands."
			),
		},
		summary=f"dismissed {len(dismissed)} alert(s) in bulk — {reason}",
		docstatus_delta="0 → 0 (updated)",
	)


# ── get_audit_readiness ─────────────────────────────────────────────────────
def get_audit_readiness(args: dict) -> ToolResult:
	"""One comparable number, and the honesty check on it."""
	_require()
	company = _company(args)
	today = as_date(args, "as_of") or frappe.utils.today()
	score = dashboard.readiness(company or "", today)

	quality = score["dismissal_quality"]
	resolved = quality["auto_dismissed"] + quality["dismissed_by_a_person"]
	by_hand = round(100.0 * quality["dismissed_by_a_person"] / resolved, 1) if resolved else 0.0

	warnings = []
	if score["by_severity"].get(alerts.SEVERITY_CRITICAL):
		warnings.append(
			f"{score['by_severity'][alerts.SEVERITY_CRITICAL]} Critical alert(s) are open. The "
			"score is a ratio and a single Critical can sit under a high one — something has "
			"stopped being lawful, whatever the percentage says."
		)
	# Two is the floor rather than one: a single resolution that happened to be a
	# dismissal says nothing about how an operation works, and crying wolf on it
	# would teach somebody to stop reading the warnings.
	if by_hand > 50 and resolved >= 2:
		warnings.append(
			f"{by_hand}% of the resolutions were dismissals somebody made by hand rather than "
			"conditions that cleared themselves. That is not necessarily wrong — a judgement is "
			"work — but an operation at this score through dismissals is a different operation "
			"from one that got the work done, and the difference is what this number is for."
		)
	if score["snoozed"]:
		warnings.append(
			f"{score['snoozed']} alert(s) are snoozed and count as open. They come back on their "
			"own; the score does not flatter them in the meantime."
		)

	return ToolResult(
		data={
			**score,
			"resolved_by_hand_percent": by_hand,
			"warnings": warnings,
			"how_it_is_computed": (
				"resolved / raised, as a percentage, where resolved means dismissed by anybody — "
				"the sweep because the condition went away, or a person because they looked and "
				"decided. Both are the work; a dismissal that was not the work is caught by the "
				"mandatory reason on it and by `resolved_by_hand_percent`."
			),
		},
		summary=(
			f"audit readiness {score['audit_readiness_score']}% "
			f"({score['resolved']}/{score['raised']} resolved, {score['open']} open, "
			f"{score['by_severity'].get(alerts.SEVERITY_CRITICAL, 0)} critical)"
		),
	)

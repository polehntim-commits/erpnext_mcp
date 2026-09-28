# SPDX-License-Identifier: MIT
"""The gate: one control, two strictnesses, one code path.

WHAT AN IPO-READINESS CONTROL IS, AND WHY IT IS NOT A SWEEP. Every Compliance
Rule this app shipped before v0.80.0 watches the site on a cadence: a nightly
scan reads records, finds the ones that have drifted, raises an alert, and
dismisses it when the drift resolves. That shape is right for a certificate
expiring in March and wrong for a spend limit, because by the time a nightly
scan notices a journal entry that exceeded somebody's authority, THE ENTRY HAS
BEEN BOOKED. A control that can only report is a control an auditor discounts.

So a control point is consulted AT THE MOMENT SOMEBODY TRIES TO DO THE THING, by
the tool that does it, before anything is written.

THE ONE DESIGN DECISION THIS MODULE EXISTS TO MAKE. Every control here is
bypassable, and the bypass is not an escape hatch bolted on afterwards — it is
the same switch, read at the same point, by the same code:

    ADVISORY   the control runs, reaches its finding, writes that finding to the
               compliance calendar as an alert against the record, and LETS THE
               WORK THROUGH.
    ENFORCED   the control runs, reaches the SAME finding, writes the SAME
               alert, and refuses — naming what was wrong and what would make it
               right.

Read those two again and notice what does not differ: the evaluation, the
finding, the alert, the audit row. An operation running Advisory for a season
accumulates EXACTLY the data trail it would have accumulated running Enforced,
minus the refusals. That is the whole point. A control whose enforced behaviour
is discovered on the morning it is switched on is a control that gets switched
off that afternoon — so the switch is preceded by a season of knowing precisely
which entries it would have stopped, on the record, in a list somebody can read.

EVERYTHING SHIPS ADVISORY. Not as timidity: as the only default that survives
contact with a four-person farm. An operation that cannot book a fuel invoice
because nobody has yet been named an approver does not conclude that its
controls need configuring — it concludes that this app is in the way, and turns
the module off entirely. An operation with the module off gets no warnings
either, which is strictly worse than an advisory control that reports the gap
and lets the truck go. This is the same argument `settings.trade_document_
enforcement` makes about a phytosanitary certificate, and it is the same answer.

WHERE THE SWITCH LIVES, AND WHY THERE ARE THREE PLACES. Precedence is most
specific first, and each layer exists because a real operator asked for it:

  1. THE RULE'S OWN `enforcement_mode`. The register an operator reads and
     edits. This is the answer for "we enforce period close but not approval
     limits yet", which is the normal shape of a company growing into this.
  2. `enabled` ON THE RULE. Off means the control does not run at all — not
     advisory, OFF. Distinct from Advisory on purpose: advisory still fills the
     calendar, and an operator who wants silence has to say so explicitly rather
     than getting it by lowering a dial.
  3. THE SITE HAS NO Compliance Rule DOCTYPE YET. A site mid-migrate. Every
     control reports `Off` and says why, rather than failing closed and locking
     an operator out of their own ledger during an upgrade.

A CONTROL POINT NOTHING IMPLEMENTS IS REFUSED AT THE DOOR. `CONTROL_POINTS` below
is the complete list, and the Compliance Rule controller checks a rule's
`control_point` against it on save. The failure this prevents is the worst one
available here: an operator reads a register that says the site enforces
segregation of duties, believes it, and tells an auditor so — while nothing
anywhere consults that row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import frappe

from . import compat
from .errors import ToolError

DOCTYPE = "Compliance Rule"
ALERT_DOCTYPE = "Compliance Alert"

#: The three answers `mode` gives. `OFF` is not a strictness — it is the absence
#: of the control, and callers branch on it before they evaluate anything, so a
#: disabled control costs nothing at transaction time.
OFF = "Off"
ADVISORY = "Advisory"
ENFORCED = "Enforced"

MODES = (ADVISORY, ENFORCED)

#: Alert severity for a finding a control produced. Advisory findings are
#: `Warning` rather than `Critical` because the work went through and somebody
#: has time; a finding that WAS blocked is raised `Critical`, since an action
#: somebody attempted and could not complete is a thing they are waiting on.
SEVERITY_ADVISORY = "Warning"
SEVERITY_BLOCKED = "Critical"

#: Which shelf of the compliance calendar these alerts land on. `Finance` is an
#: existing Compliance Alert category (v0.39.0, the KPI framework) and not a new
#: one, deliberately: an operation with two alerting boards reads neither.
CATEGORY = "Finance"


@dataclass(frozen=True)
class ControlPoint:
	"""One transaction-time control this app actually implements.

	`title`, `purpose` and `citation` are copied onto the seeded Compliance Rule,
	so the register an operator reads and the table this module dispatches on
	cannot drift into describing different controls.
	"""

	key: str
	title: str
	purpose: str
	citation: str
	#: What the control refuses, in the imperative, for the enforced message.
	blocks: str
	#: The doctype whose rows this control READS to reach its verdict, copied onto
	#: the seeded rule's mandatory `target_doctype`. It is the register the gate
	#: consults, not necessarily the document somebody was trying to write: the
	#: approval-threshold gate reads Approval Threshold rows to judge a Payment
	#: Entry, and it is the threshold table an operator wants named when they ask
	#: what that row is about. Where the two coincide — the three journal-entry
	#: controls, which read the entry itself — they are the same doctype.
	#:
	#: A gate is never scanned, so this field steers nothing at runtime. What it
	#: does is make `describe`'s `requires`/`available` honest (a site without
	#: Backup Record cannot verify a restore, and the register should say so
	#: rather than showing a control that silently never fires) and give the
	#: mandatory column on Compliance Rule a true value rather than a placeholder.
	target_doctype: str


#: THE COMPLETE LIST. A rule naming anything else is refused on save.
#:
#: The citations are the internal-control frameworks a readiness exercise is
#: actually measured against — COSO's 2013 framework for the control environment
#: and SOX §404 for management's assertion about it. They are pointers for the
#: person assembling the binder, not legal advice, and they are on the record
#: rather than in a consultant's spreadsheet because that is the difference
#: between a control an auditor can test and a control somebody describes.
CONTROL_POINTS = {
	"approval_threshold": ControlPoint(
		key="approval_threshold",
		title="A transaction exceeds the approval authority of the person booking it",
		purpose=(
			"Spending authority is the control an auditor tests first, because it is the "
			"one every operation claims to have and few can evidence. A threshold that "
			"lives in somebody's head is not a control; a threshold that is a record, "
			"consulted before the entry is written, is."
		),
		citation="COSO 2013 Principle 10 (Control Activities); SOX §404",
		blocks="booking a transaction above the approval authority that covers it",
		target_doctype="Approval Threshold",
	),
	"period_close_lockdown": ControlPoint(
		key="period_close_lockdown",
		title="A posting is dated into an accounting period that has been closed",
		purpose=(
			"A closed period whose numbers can still move is a closed period in name "
			"only. Every comparative report, every filed return and every figure given "
			"to a lender is a claim about a month that nobody has posted into since — "
			"and a single back-dated entry falsifies all of them at once, silently, "
			"because nothing re-runs."
		),
		citation="COSO 2013 Principle 12 (Control Activities); SOX §404",
		blocks="posting into a period that has been closed",
		target_doctype="Journal Entry",
	),
	"closing_checklist": ControlPoint(
		key="closing_checklist",
		title="A period is being closed with required checklist steps outstanding",
		purpose=(
			"Closing is a sequence — reconcile the bank, post accruals, run "
			"depreciation — and the failure is never the step somebody refused to do. "
			"It is the step nobody remembered in the week the packing line broke. The "
			"checklist is what makes 'we close consistently' a testable claim rather "
			"than a description of a good month."
		),
		citation="COSO 2013 Principle 12 (Control Activities); SOX §404",
		blocks="closing a period with required steps still outstanding",
		target_doctype="Closing Checklist",
	),
	"journal_entry_duplicate": ControlPoint(
		key="journal_entry_duplicate",
		title="A journal entry duplicates one already on the books",
		purpose=(
			"The same accrual posted twice because two people were closing the same "
			"month is the commonest error in a small finance function, and it is "
			"invisible: both entries are correct, balanced and well-described. Only "
			"their coexistence is wrong, and only something comparing them at the "
			"moment of writing can see it."
		),
		citation="COSO 2013 Principle 10 (Control Activities)",
		blocks="booking an entry that duplicates one already on the books",
		target_doctype="Journal Entry",
	),
	"journal_entry_unusual_amount": ControlPoint(
		key="journal_entry_unusual_amount",
		title="A journal entry is far outside the normal size for its accounts",
		purpose=(
			"A transposed digit produces a balanced, well-formed entry that is wrong "
			"by an order of magnitude, and nothing about its shape gives it away. "
			"Comparing an entry against what these accounts normally carry is the "
			"cheapest detective control available and the one most likely to catch a "
			"keystroke before it reaches a statement."
		),
		citation="COSO 2013 Principle 10 (Control Activities)",
		blocks="booking an entry far outside the normal size for its accounts",
		target_doctype="Journal Entry",
	),
	"segregation_of_duties": ControlPoint(
		key="segregation_of_duties",
		title="The same person prepared and approved a transaction",
		purpose=(
			"One person who can both write an entry and release it is the whole of "
			"what segregation of duties exists to prevent, and it is the finding an "
			"auditor raises about small finance functions every single time. On a farm "
			"it is often unavoidable — which is an argument for RECORDING it as a known "
			"and compensated exception, not for failing to notice."
		),
		citation="COSO 2013 Principle 10 (Control Activities); SOX §404",
		blocks="approving a transaction the same person prepared",
		target_doctype="Journal Entry",
	),
	"revenue_recognition": ControlPoint(
		key="revenue_recognition",
		title="Revenue is being recognised against an unsatisfied performance obligation",
		purpose=(
			"ASC 606 recognises revenue when control of the goods transfers, not when "
			"cash arrives and not when a contract is signed. A schedule that can be "
			"drawn against before the obligation it names is satisfied is a schedule "
			"that will restate."
		),
		citation="ASC 606-10-25 (Revenue from Contracts with Customers)",
		blocks="recognising revenue against an obligation that is not yet satisfied",
		target_doctype="Revenue Contract",
	),
	"cost_variance": ControlPoint(
		key="cost_variance",
		title="Actual cost has diverged from standard by more than the tolerated band",
		purpose=(
			"A standard cost nobody compares against is a number in a table. The "
			"variance is the control: it is what turns 'we know what a bin should cost' "
			"into 'we know what a bin cost, and why the two differ'."
		),
		citation="COSO 2013 Principle 16 (Monitoring Activities)",
		blocks="accepting a cost variance beyond the tolerated band without explanation",
		target_doctype="Standard Cost",
	),
	# ── v0.81.0: the governance domain ───────────────────────────────────────
	# The five below are consulted by tools/related_party_controls.py,
	# tools/itgc.py and tools/disclosure.py. They ship in the same release as
	# those consumers, deliberately: a control point seeded ahead of the code
	# that reads it would put a row in the register saying this site tracks
	# something nothing anywhere consults, which is the failure this module's
	# docstring calls the worst one available.
	"access_review": ControlPoint(
		key="access_review",
		title="Privileged access has not been reviewed within the review period",
		purpose=(
			"Access accumulates. The bookkeeper who covered payroll for one season keeps "
			"the role for four more years, the packing-house temp still has a login, and "
			"nobody decided any of it — which is the point: an access list is not wrong at "
			"any moment, it is wrong by drift. A periodic review is the only control that "
			"catches a permission nobody would grant today and nobody remembers granting."
		),
		citation="COBIT 2019 DSS05.04 (Managed Identity and Logical Access); COSO 2013 Principle 11; SOX §404",
		blocks="granting or widening access when the last review is older than the review period",
		target_doctype="User",
	),
	"backup_verification": ControlPoint(
		key="backup_verification",
		title="No verified backup or test restore within the required window",
		purpose=(
			"An unverified backup is a belief, not a control. The failure it exists to "
			"prevent is discovered exactly once, at the worst possible moment, and the "
			"discovery is always the same sentence: the job had been green for months and "
			"the restore did not work. What makes a backup a control is a RESTORE somebody "
			"actually performed and dated — which is why this control reads the test "
			"restore and not the job log."
		),
		citation="COBIT 2019 DSS04.07 (Managed Continuity); COSO 2013 Principle 11; SOX §404",
		blocks="declaring recovery readiness when no backup has been verified inside the required window",
		target_doctype="Backup Record",
	),
	"change_approval": ControlPoint(
		key="change_approval",
		title="A system change was recorded with nobody named as its approver",
		purpose=(
			"Change management is the ITGC an auditor tests by sampling: pick five changes, "
			"show me who approved each. An operation that cannot answer for one of the five "
			"has not failed that change — it has failed the control, for every change. The "
			"record has to name a person, and the person has to not be the one who made it."
		),
		citation="COBIT 2019 BAI06.01 (Managed IT Changes); COSO 2013 Principle 11; SOX §404",
		blocks="recording a system change with no approver, or with the person who made it as its own approver",
		target_doctype="Change Management Log",
	),
	"disclosure_completeness": ControlPoint(
		key="disclosure_completeness",
		title="A filing is being finalised with required disclosures outstanding",
		purpose=(
			"A disclosure checklist exists because the omission is never the disclosure "
			"somebody refused to make — it is the one nobody was assigned. Completeness is "
			"therefore a control about the LIST rather than about any item on it, and the "
			"moment it has to be read is the moment somebody calls the filing done, because "
			"after that the checklist is a historical document."
		),
		citation="SEC Regulation S-K; SOX §302 (Disclosure Controls and Procedures)",
		blocks="marking a filing complete while required disclosure items are still outstanding",
		target_doctype="Disclosure Checklist",
	),
	"related_party_transfer_pricing": ControlPoint(
		key="related_party_transfer_pricing",
		title="A related-party transaction has no arm's-length documentation behind it",
		purpose=(
			"A payment to the manager's own trucking LLC is not wrong. A payment to the "
			"manager's own trucking LLC that nobody priced against the market is the "
			"finding — and it is found years later, by somebody who was not there, with the "
			"burden of proof reversed. The documentation is cheap on the day of the dealing "
			"and nearly impossible to reconstruct afterwards, which is the entire argument "
			"for a control at the transaction rather than a review at year end."
		),
		citation="IRC §482 and Treas. Reg. §1.482-1 (arm's length standard); ASC 850 (Related Party Disclosures)",
		blocks="booking a related-party transaction with no transfer pricing documentation covering it",
		target_doctype="GL Entry",
	),
	# v0.203.0. The first control point that is not about money. Rodent bait
	# INSIDE a housing unit is the highest-exposure use of a rodenticide, and a
	# unit carrying it must not be assigned to anyone until a Rodent Bait Removal
	# and Clearance is COMPLETED there (docs/design/rodent_bait_program.md §8).
	"housing_preoccupancy_bait_clearance": ControlPoint(
		key="housing_preoccupancy_bait_clearance",
		title="A housing unit is being assigned while interior rodent bait there is not cleared",
		purpose=(
			"Interior bait where people sleep is within reach of children, pets and food. The "
			"pre-occupancy clearance — every station removed, leftover bait and carcasses "
			"collected, surfaces photographed clean — is what makes the unit fit to hand over."
		),
		citation=(
			"Product label directions (FIFRA §12(a)(2)(G)); 29 CFR 1910.142(j) insect and rodent "
			"control in temporary labor camps; OAR 437-004-1120"
		),
		blocks="assigning a housing unit that still has uncleared interior rodent bait",
		target_doctype="Farm Task",
	),
}


@dataclass
class Finding:
	"""One thing a control found wrong with one record.

	`remedy` is not decoration. A refusal that does not say what would make the
	action succeed teaches a caller — often a language model — to retry the same
	call, and a control that produces retry loops gets disabled. Every finding
	carries the sentence that ends the loop.
	"""

	control_point: str
	message: str
	remedy: str = ""
	source_doctype: str = ""
	source_docname: str = ""
	company: str = ""
	#: Whatever the control wants a reader to have: the amount, the threshold it
	#: crossed, the duplicate's docname. Serialised into the response, never into
	#: the alert message, so the sentence a person reads stays a sentence.
	detail: dict = field(default_factory=dict)

	def as_dict(self) -> dict:
		return {
			"control_point": self.control_point,
			"control": (
				CONTROL_POINTS[self.control_point].title if self.control_point in CONTROL_POINTS else ""
			),
			"message": self.message,
			"remedy": self.remedy,
			"source_doctype": self.source_doctype or None,
			"source_docname": self.source_docname or None,
			"company": self.company or None,
			"detail": dict(self.detail),
		}


def require_control_point(key: str) -> str:
	"""A control point key this app implements, or a ToolError naming the list."""
	key = str(key or "").strip()
	if not key:
		raise ToolError("control_point is required")
	if key not in CONTROL_POINTS:
		raise ToolError(
			f"{key!r} is not a control point this app implements. Known control points: "
			f"{', '.join(sorted(CONTROL_POINTS))}. A rule naming a control nothing consults "
			"would tell an operator their site enforces something it does not, which is worse "
			"than having no rule at all."
		)
	return key


def rule_row(control_point: str) -> dict:
	"""The live Compliance Rule governing one control point, or `{}`.

	LIVE MEANS ENABLED AND UNSUPERSEDED, matching `compliance_rules.rule_rows`.
	A superseded row is the definition a past finding was raised under and must
	not govern today's; a disabled row is a decision somebody made this season.
	"""
	if not compat.doctype_exists(DOCTYPE):
		return {}
	if not compat.has_field(DOCTYPE, "control_point"):
		# The doctype is here but predates v0.80.0 — a site that has migrated the
		# app partially. Reporting Off is right: there is nowhere for an operator
		# to have expressed an opinion, so we have not overridden one.
		return {}
	fields = compat.existing_fields(
		DOCTYPE,
		("name", "rule_id", "title", "enforcement_mode", "enabled", "purpose", "regulation_citations"),
	)
	rows = frappe.db.get_all(
		DOCTYPE,
		filters={"control_point": control_point, "enabled": 1, "superseded_by": ("in", ("", None))},
		fields=fields,
		order_by="version desc",
		limit=1,
	)
	return dict(rows[0]) if rows else {}


def mode(control_point: str) -> str:
	"""`Off`, `Advisory` or `Enforced` for one control point on this site.

	Never raises. A control that cannot read its own switch reports `Off` — the
	alternative is a site whose ledger locks up because a rule row is malformed,
	and a control that fails closed on its own bug is worse than the risk it
	watches for.
	"""
	try:
		row = rule_row(control_point)
	except Exception:  # pragma: no cover - a site whose rule table is unreadable
		return OFF
	if not row:
		return OFF
	value = str(row.get("enforcement_mode") or "").strip()
	return ENFORCED if value == ENFORCED else ADVISORY


def status(control_point: str) -> dict:
	"""One control's switch, as a tool reports it.

	Shared by every tool that runs a control, so the block of keys a caller reads
	to find out what happened is the same shape whichever control produced it.
	"""
	require_control_point(control_point)
	row = rule_row(control_point)
	current = mode(control_point)
	spec = CONTROL_POINTS[control_point]
	return {
		"control_point": control_point,
		"control": spec.title,
		"mode": current,
		"enforced": current == ENFORCED,
		"rule": row.get("name") or None,
		"citation": spec.citation,
		"note": _mode_note(current, spec),
	}


def _mode_note(current: str, spec: ControlPoint) -> str:
	if current == ENFORCED:
		return (
			f"ENFORCED. This control refuses {spec.blocks}, and the refusal names what would "
			"make the action succeed. Turn it back to Advisory on the Compliance Rule to keep "
			"the finding and lose the refusal."
		)
	if current == ADVISORY:
		return (
			f"ADVISORY. This control still evaluates {spec.blocks} and still files what it finds "
			"to the compliance calendar — it does not refuse. The findings it raises are exactly "
			"the ones it WOULD have blocked on, which is what makes turning it up a decision with "
			"evidence behind it rather than a leap."
		)
	return (
		"OFF. No live Compliance Rule governs this control point, so nothing was evaluated and "
		"nothing was filed. seed_compliance_rules installs one in Advisory mode; enabling it "
		"there is what turns the control on."
	)


def evaluate(control_point: str, findings, *, company: str = "", raise_on_enforced: bool = True) -> dict:
	"""Run one control's findings through the switch. THE ONLY WAY A CONTROL ACTS.

	Every control in Phases 1 to 3 ends here, which is what makes the promise in
	the module docstring checkable rather than aspirational: there is one place
	that decides between reporting and refusing, so no control can grow a second
	opinion about what Advisory means.

	Returns the block a tool merges into its response. Raises `ToolError` when the
	mode is Enforced and there is anything to report — unless `raise_on_enforced`
	is False, which is how the *_controls read tools show a caller what enforcement
	WOULD do without doing it.

	CALL THIS BEFORE THE TOOL WRITES ANYTHING. On the enforced path `_file_alerts`
	commits, so that the alert survives the rollback `dispatch` performs on a
	`ToolError` — and a commit persists the whole open transaction. A control
	evaluated after its caller had already written would therefore save exactly
	the half-built document that rollback exists to discard. See `_file_alerts`.
	"""
	block = status(control_point)
	findings = list(findings or [])
	block["findings"] = [finding.as_dict() for finding in findings]
	block["finding_count"] = len(findings)
	block["clear"] = not findings

	if block["mode"] == OFF or not findings:
		block["action"] = "none"
		return block

	blocked = block["mode"] == ENFORCED and raise_on_enforced
	# THE ALERT IS WRITTEN EITHER WAY, and that is the data-trail promise. An
	# operation that spends a season in Advisory ends it holding the same register
	# of findings it would hold had it been enforcing, which is the evidence that
	# makes the switch a decision instead of a gamble.
	written = _file_alerts(control_point, findings, company=company, blocked=blocked)
	block["alerts"] = written
	block["action"] = "blocked" if blocked else "reported"

	if not blocked:
		block["advisory_note"] = (
			f"{len(findings)} finding(s) were REPORTED AND ALLOWED THROUGH because this control "
			f"is Advisory. Under Enforcement this call would have been refused. "
			+ (f"{len(written)} compliance alert(s) carry the detail. " if written else "")
			+ "Nothing here is a reason to re-try the call differently — the work was done."
		)
		return block

	raise ToolError(_refusal(control_point, findings, written))


def _refusal(control_point: str, findings: list, written: list) -> str:
	spec = CONTROL_POINTS[control_point]
	lines = [
		f"REFUSED by the {spec.title!r} control, which this site has set to Enforced. Nothing was written."
	]
	for index, finding in enumerate(findings, start=1):
		prefix = f"({index}) " if len(findings) > 1 else ""
		lines.append(f"{prefix}{finding.message}" + (f" {finding.remedy}" if finding.remedy else ""))
	lines.append(
		"To let this through: fix what is named above, or set this control's Compliance Rule "
		f"back to Advisory with update_compliance_rule — it will then report the same finding "
		f"and allow the work. Citation: {spec.citation}."
	)
	if written:
		lines.append(f"Filed as compliance alert(s): {', '.join(written)}.")
	return " ".join(lines)


def _file_alerts(control_point: str, findings: list, *, company: str = "", blocked: bool = False) -> list:
	"""Write one Compliance Alert per finding. Never fails the caller.

	A control that could not file its alert has still reached its finding, and
	the finding is in the response either way. Losing the ledger write because the
	calendar was unwritable would be the wrong trade in both directions: in
	Advisory it would silently discard the evidence the mode exists to gather, and
	in Enforced it would turn a refusal into a traceback.

	THE BLOCKED PATH COMMITS, AND WITHOUT THAT THIS MODULE'S CENTRAL PROMISE IS
	FALSE. `registry.dispatch` catches `ToolError` and calls `frappe.db.rollback()`
	before it logs — deliberately, so a half-built document cannot be committed by
	the framework at the end of a failed request. An alert written moments before
	`evaluate` raises is inside that transaction and goes with it. The result
	would be an app that keeps the evidence when a control merely advised and
	DISCARDS IT WHEN THE CONTROL ACTUALLY FIRED — inverting the docstring's claim
	that the data trail is identical, precisely for the refusal an operator will
	later be asked to explain.

	This is the same argument `audit.record(commit=True)` makes about an audit row
	inserted before a rollback, and it takes the same answer.

	WHICH IS WHY A GATE MUST BE CONSULTED BEFORE ITS TOOL WRITES ANYTHING. A
	commit here commits the WHOLE open transaction, so a control evaluated after
	its caller had already written would persist exactly the half-built document
	the rollback exists to discard. Every call site in Phases 1 to 3 evaluates
	first and writes second, and that ordering is a requirement of placing a
	control, not a stylistic preference.

	ADVISORY DOES NOT COMMIT, and that asymmetry is correct rather than an
	oversight. Nothing raises on that path, so the framework commits the request
	as it always would; forcing one here would break the caller's own transaction
	boundary for no gain.
	"""
	if not compat.doctype_exists(ALERT_DOCTYPE):
		return []
	spec = CONTROL_POINTS[control_point]
	severity = SEVERITY_BLOCKED if blocked else SEVERITY_ADVISORY
	written = []
	for finding in findings:
		try:
			written.append(_upsert_alert(control_point, spec, finding, severity, company, blocked))
		except Exception:  # pragma: no cover - an unwritable calendar
			continue
	written = [name for name in written if name]
	if blocked and written:
		try:
			frappe.db.commit()
		except Exception:  # pragma: no cover - a poisoned transaction
			# Swallowed for the same reason `audit.record` swallows its own
			# failure: the refusal is the important half and it must still reach
			# the caller as a sentence rather than as a traceback about the
			# calendar.
			pass
	return written


def _upsert_alert(
	control_point: str,
	spec: ControlPoint,
	finding: Finding,
	severity: str,
	company: str,
	blocked: bool,
) -> str:
	"""Create or refresh the one alert this finding owns.

	KEYED THE WAY THE SWEEP KEYS ITS OWN, through `alerts.base.alert_key`, so a
	gate finding and a swept finding sit in one calendar under one naming scheme
	and `list_compliance_calendar` needs to know nothing about the difference. The
	key carries nothing that changes between evaluations — not the amount, not the
	severity — because a key that moved would write a duplicate every time
	somebody retried the call, and orphan any snooze a person had set.
	"""
	from .alerts import base as alerts

	source_doctype = finding.source_doctype or ""
	source_docname = finding.source_docname or ""
	key = alerts.alert_key(control_point, source_doctype or "Site", source_docname or "control")
	message = finding.message
	if finding.remedy:
		message = f"{message} {finding.remedy}"
	message = f"{message} [{'blocked' if blocked else 'advisory — the work was allowed through'}]"
	company = finding.company or company or ""

	if frappe.db.exists(ALERT_DOCTYPE, key):
		doc = frappe.get_doc(ALERT_DOCTYPE, key)
		doc.severity = severity
		doc.alert_message = message
		doc.company = company or None
		doc.last_refreshed = frappe.utils.now()
		# A finding that has come back after somebody auto-dismissed it reopens,
		# exactly as the sweep's does. A HUMAN dismissal is left alone: somebody
		# looked at this and decided, and a control noticing the same thing again
		# does not get to overrule them.
		if frappe.utils.cint(doc.get("auto_dismissed")):
			doc.dismissed = 0
			doc.auto_dismissed = 0
			doc.dismissed_by = None
			doc.dismissed_on = None
			doc.dismissed_reason = None
		doc.save(ignore_permissions=True)
		return doc.name

	doc = frappe.new_doc(ALERT_DOCTYPE)
	doc.alert_key = key
	doc.alert_type = control_point
	doc.severity = severity
	doc.category = CATEGORY
	doc.company = company or None
	doc.source_doctype = source_doctype or None
	doc.source_docname = source_docname or None
	doc.alert_message = message
	doc.first_seen = frappe.utils.today()
	doc.last_refreshed = frappe.utils.now()
	# `can_dismiss` stays at the doctype default. A control finding is not
	# something to wave away from a phone in a packing shed, which is what that
	# flag governs.
	doc.insert(ignore_permissions=True)
	return doc.name


def seed_specs() -> list:
	"""One Compliance Rule spec per control point, ALL OF THEM ADVISORY.

	Seeded rather than fixtured, for the reason `compliance_rules.py` gives about
	every other rule: a fixture is reapplied on every migrate, so an operator who
	turned enforcement ON in March would have it turned back off by the next
	upgrade. These are records. `install.py` writes the ones that are missing and
	never touches one that is already there.

	`enabled` is 1 and `enforcement_mode` is Advisory, which together mean: the
	control runs from the first migrate, files what it finds, and refuses nothing.
	An operation therefore starts accumulating the evidence for its own switch
	decision immediately, without anybody configuring anything — and an operation
	that wants silence rather than advice unticks `enabled`, which is a different
	and louder decision than lowering a dial.
	"""
	specs = []
	for key in sorted(CONTROL_POINTS):
		spec = CONTROL_POINTS[key]
		specs.append(
			{
				"rule_id": f"control_{key}",
				"title": spec.title,
				"category": CATEGORY,
				"control_point": key,
				# Mandatory on Compliance Rule since long before these gates existed,
				# and left unfilled by every one of them from v0.80.0 to v0.87.0. The
				# omission was easy to make and impossible to see: a gate is never
				# scanned, so nothing in THIS module ever needed a target doctype, and
				# the standalone harness does not enforce `reqd`. On a real bench all
				# thirteen raised MandatoryError — into `seed_compliance_rules`'s
				# `failed` list, which is swallowed by design because the seeder runs
				# inside `bench migrate` and must never abort it. So the entire IPO
				# readiness register was simply absent from every site, quietly, which
				# is precisely the outcome this module's docstring calls the worst one
				# available.
				"target_doctype": spec.target_doctype,
				"enforcement_mode": ADVISORY,
				"enabled": 1,
				"purpose": spec.purpose,
				"regulation_citations": spec.citation,
				"kairotic_gate_description": (
					"AT THE MOMENT OF THE TRANSACTION, not on a nightly sweep. This rule is a "
					"GATE: the tool that performs the action consults it before writing "
					f"anything, and what it governs is {spec.blocks}. Advisory reports and "
					"allows; Enforced refuses and explains. Nothing about this rule is scanned, "
					"so it raises nothing on a quiet day and everything on a busy one."
				),
				"authored_by": "System",
				"regimes": ["Internal"],
				"retention_years": 7,
			}
		)
		if key in _NON_FINANCE:
			# v0.203.0. Seeded OFF: the rodent bait program is Tim's to switch on,
			# with the templates it depends on. Housing, not Finance.
			specs[-1].update(_NON_FINANCE[key])
	return specs


#: Control points outside the finance/ITGC set, and what their seeded rule differs in.
_NON_FINANCE = {
	"housing_preoccupancy_bait_clearance": {
		"category": "Housing",
		"enabled": 0,
		"regimes": ["OR-OSHA", "Internal"],
		"retention_years": 3,
	},
}


def describe_all() -> list:
	"""Every control point with its live switch — the read behind the register."""
	out = []
	for key in sorted(CONTROL_POINTS):
		spec = CONTROL_POINTS[key]
		row = rule_row(key)
		current = mode(key)
		out.append(
			{
				"control_point": key,
				"control": spec.title,
				"purpose": spec.purpose,
				"citation": spec.citation,
				"blocks": spec.blocks,
				"target_doctype": spec.target_doctype,
				"mode": current,
				"enforced": current == ENFORCED,
				"rule": row.get("name") or None,
			}
		)
	return out


def as_json(value) -> str:
	"""A JSON blob for a Long Text column, with the empty case spelled the same.

	Small, and here rather than in each tool module, because three doctypes in
	Phases 1 to 3 carry a JSON column and three different spellings of "nothing
	yet" (`None`, `""`, `"{}"`) is how a report ends up branching on which tool
	wrote a row.
	"""
	return json.dumps(value if value is not None else {})

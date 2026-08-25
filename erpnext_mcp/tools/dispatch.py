# SPDX-License-Identifier: MIT
"""Farm Task Dispatch: the pool, the board, and the bridge from the calendar.

Sprint 7 built a compliance calendar that could tell an operation fifty-four
things were wrong. It could not tell anybody to go and fix one. This is the half
that can — and it is built compliance-native from the first field rather than
having compliance added to it afterwards, which is a distinction with three
concrete consequences:

  1. **A task cannot be created without saying what closing it requires.**
     `evidence_required` is mandatory on the doctype. There is no path to a task
     that somebody can close by saying they did it.
  2. **Completing a task WRITES THE COMPLIANCE RECORD.** Not a status change with
     a note about one — the actual Housing Inspection, Detector Test or Water
     Test, with the photographs attached to it, which then moves the register
     forward and lets the alert dismiss itself on the next sweep.
  3. **Rejection is a first-class state with a mandatory reason.** "Nobody got to
     it and dispatch never followed up" is the answer that cannot be defended.
     `reject_farm_task` turns it into "the ladder is broken and I could not reach
     the detector", which is a fact somebody can act on, on a record that stays.

DUAL MODE, BECAUSE ONE MODE IS WRONG FOR HALF THE WORK. A habitability walk is
general labour: anybody with camp maintenance skills can take it from the pool,
and making a foreman assign fifty-four of them by hand is how fifty-four of them
do not happen. Fitting a CO detector, spraying under an applicator licence, or
anything where the named holder matters is dispatched: somebody is SENT, by name,
and the record says who sent them. `dispatch_mode` is the field, and `Either`
opens both doors for the majority of work where both are fine.

THE CONCURRENT-CLAIM LIMIT IS A HOARDING LIMIT AND NOT A PRODUCTIVITY ONE. Three
at once is a morning: enough to plan a trip round the camp, few enough that
nobody can pull the whole pool onto their own name and leave a board that looks
worked. Completing one frees a slot in the same instant, so it never stands
between somebody and their next job — it only stands between them and their
fourth simultaneous one.

WHAT `complete_farm_task` REFUSES, AND WHY EACH REFUSAL EARNED ITS PLACE:

  * a submission that does not meet the task's evidence contract — the entire
    reason the contract is mandatory at creation;
  * a worker who is not the one holding the task — a completion filed by
    somebody who was not there is not chain of custody, it is a rumour;
  * a task nobody has claimed, and a task already finished.

It does NOT refuse a completion whose findings are alarming, a completion that
took four times the estimate, or a completion of work that is months overdue.
Every one of those is a fact worth recording, and a tool that refused to record
it would guarantee the record stayed empty.

AWAITING-REVIEW IS NOT A SECOND APPROVAL STEP. A completion lands there when the
compliance record it produced found something — a water stain, a dead detector, a
coliform count. The work IS done and the register IS updated; what needs a human
is the finding, and the Critical alert raised against the record is how they hear
about it. A completion that found nothing goes straight to Completed, because
routing clean work through a review queue is how a review queue stops being read.
"""

from __future__ import annotations

import json

import frappe

from .. import alerts, compat, completions, datetimes, minors, records, sessions, training_sessions
from ..args import as_bool, as_choice, as_int, as_limit, as_str, as_visit_id, resolve_company
from ..erpnext_mcp.doctype.farm_task.farm_task import (
	AVAILABLE,
	AWAITING_REVIEW,
	CANCELLED,
	CLAIMED,
	COMPLETED,
	DISPATCH_DISPATCHED,
	DRAFT,
	EVIDENCE_KEYS,
	IN_PROGRESS,
	MAX_CONCURRENT_CLAIMS,
	MERGED,
	ORIGIN_COMPLIANCE_RULE,
	ORIGIN_FIELD_REPORTED,
	PAUSED,
	REJECTED,
	SELF_PICKABLE,
	STATES,
	TERMINAL_STATES,
	checklist_items,
	evidence_contract,
	parse_json_object,
	unmet_checklist,
)
from ..erpnext_mcp.doctype.farm_task_assignment.farm_task_assignment import (
	concurrent_claims,
	live_assignment,
)
from ..errors import ToolError
from ..result import ToolResult
from ..services import push as push_service
from . import inspections, scouting, shadow_log
from .housing import EMPLOYEE, hr_installed

FARM_TASK = "Farm Task"
FARM_TASK_ASSIGNMENT = "Farm Task Assignment"

#: The one `task_type` whose completion reads a tank mix off the task and stamps
#: a restricted-entry and a pre-harvest window from it. Named once here because
#: three places test it and a literal in each is how the fourth gets missed.
SPRAY_TASK_TYPE = "Spray"
ALERT = alerts.ALERT_DOCTYPE

#: The alert type whose several rows become ONE afternoon, and the register it
#: points at. v0.98.0 — see `_bundle_into_training_sessions`. Named here rather
#: than spelled inline for the reason `SPRAY_TASK_TYPE` is: three places test it.
TRAINING_ALERT = "training_expiring"
TRAINING_RECORD = "Employee Training Record"

BOARD_CAP = 500

#: Most tasks one `generate_tasks_from_compliance_alerts` call will create. The
#: same number the alert engine caps a single rule at, for the same reason: past
#: it, something is firing on a field that is empty everywhere rather than stale
#: on a few, and turning that into five hundred dispatchable tasks would bury the
#: dozen that matter.
GENERATE_CAP = 500

_TASK_FIELDS = (
	"name",
	"task_name",
	"task_type",
	"state",
	"urgency",
	"company",
	"location_doctype",
	"location",
	"skill_required",
	"dispatch_mode",
	"assigned_to",
	"assigned_to_name",
	"estimated_duration_minutes",
	"source_alert",
	"source_workorder",
	"evidence_required",
	"creates_record",
	"creates_record_data",
	"produced_record",
	"notes",
	"asset",
	"template",
	"checklist_status",
	"farm_shift",
	# v0.69.0. The tank mix, what issuing it did, and the two windows a spray
	# leaves behind. Read here rather than fetched per call: `task_row` is what
	# every describe, every replay and the drawdown itself already reads.
	"materials_used",
	"stock_drawdown",
	"spray_completed_at",
	"rei_expires_at",
	"rei_source_item",
	"phi_clears_on",
	"phi_source_item",
	"creation",
	"modified",
	"owner",
)

_ASSIGNMENT_FIELDS = (
	"name",
	"task",
	"task_name",
	"assigned_to",
	"assigned_to_name",
	"state",
	"company",
	"dispatched_by_foreman",
	"claimed_at",
	"started_at",
	"completed_at",
	"actual_duration_minutes",
	"completion_narrative",
	"findings_text",
	"witness",
	"farm_location_gps",
	"rejection_reason",
	"signature_file",
	"produced_record",
	"visit_id",
	"completion_signature",
	"farm_shift",
	"creation",
	"owner",
)

#: v0.64.0. The Farm Shift doctype, named here rather than imported from
#: `shifts.py` at module scope because dispatch must keep loading on a site that
#: has not migrated the shift tables — every read of it below goes through
#: `compat.doctype_exists` first, and a task surface that refused to import
#: because the shift surface was absent would take out the dispatch board too.
FARM_SHIFT = "Farm Shift"

#: The event a completion writes onto the shift it was done on. Its own type
#: rather than `Other`, because a timeline where every dispatched job reads
#: "Other" is a timeline nobody can scan — and scanning it is the entire reason
#: an inspector opens a shift record.
TASK_COMPLETED_EVENT = "Task Completed"


# ── shared ──────────────────────────────────────────────────────────────────
def _require(doctype: str = FARM_TASK) -> None:
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _company(args: dict) -> str | None:
	return resolve_company(as_str(args, "company"), required=False)


def task_row(task: str) -> dict:
	task = (task or "").strip()
	if not task:
		raise ToolError("task is required (a Farm Task docname such as 'FT-2026-07-00012').")
	if not frappe.db.exists(FARM_TASK, task):
		raise ToolError(
			f"no Farm Task called {task!r} on this site. list_dispatch_board has everything that is "
			"open. Nothing was changed."
		)
	return dict(
		frappe.db.get_value(FARM_TASK, task, compat.existing_fields(FARM_TASK, _TASK_FIELDS), as_dict=True)
		or {}
	)


def lock_task(task: str) -> None:
	"""Hold the Farm Task row until this transaction ends. THE CLAIM RACE'S FIX.

	────────────────────────────────────────────────────────────────────────
	S8: TWO WORKERS, ONE TASK, AND BOTH TOLD THEY GOT IT
	────────────────────────────────────────────────────────────────────────

	`claim_farm_task` read the state, compared it with `Available`, and then
	saved — three statements with two gaps in them. Two handsets that tap the
	same job inside the same moment both read `Available`, both pass the check,
	and both write; the second `save` overwrites `assigned_to` and the FIRST
	worker is told, in a 200, that the task is theirs. Nothing in the record
	afterwards shows there were two: one name is on the task, one assignment row
	exists, and the other person is standing in the block holding a phone that
	says they own it. That is the exact failure a dispatch board exists to
	prevent, and the refusal `claim_farm_task` already writes for the sequential
	case says so in those words — it simply never ran for the simultaneous one.

	A SELECT ... FOR UPDATE ON THE ROW, WHICH IS THE WHOLE MECHANISM. Frappe wraps
	each request in one transaction, so the lock this takes is held until that
	request commits. The second claimer BLOCKS here rather than reading stale
	state, and when it wakes the row says `Claimed` and it takes the ordinary
	refusal with the holder's name in it. No new failure mode, no polling, no
	retry — the existing sentence, now reachable.

	NOT AN `UPDATE ... WHERE state = 'Available'`, which is the other way to make
	this atomic. `tests_standalone/harness.py` refuses raw SQL by design — "every
	write goes through the ORM so doctype validation runs" — and a claim that
	bypassed the controller would skip whatever `Farm Task.validate` grows next.
	The lock keeps the ORM write and makes the read before it authoritative.

	THE HARNESS IGNORES `for_update` AND THAT IS CORRECT. The double is
	single-threaded and has no locks to take; what the tests can assert is that
	the state is re-read after this call, which
	`test_dispatch.TwoWorkersOneTask` does by changing the row in between. The
	`TypeError` fallback is for a Frappe old enough not to accept the argument:
	losing the lock is bad, but refusing every claim on that bench is worse.
	"""
	try:
		frappe.db.get_value(FARM_TASK, task, "name", for_update=True)
	except TypeError:  # pragma: no cover - a Frappe without for_update
		pass


def _shift_argument(args: dict, company: str = "", key: str = "farm_shift") -> str:
	"""A Farm Shift docname off the arguments, checked, or "" where none was given.

	v0.64.0. REFUSES A SHIFT THAT DOES NOT EXIST AND A SHIFT AT ANOTHER COMPANY,
	and does neither silently. The link is what puts a completion's evidence onto
	a record spanning the whole exposure period, so a typo here does not produce a
	task with a slightly wrong field — it produces a task whose evidence lands on
	nobody's shift, or worse, on a crew that was never there.

	A CLOSED SHIFT IS ACCEPTED AND REPORTED RATHER THAN REFUSED. Work is written
	up after the fact constantly — the phone that could not reach the site until
	the evening is the same caller `log_shift_location` keeps — and refusing to
	record which shift a job was done on because the foreman has already signed
	the close would throw away the link precisely when it is hardest to
	reconstruct.
	"""
	name = as_str(args, key) or as_str(args, "shift")
	if not name:
		return ""
	if not compat.doctype_exists(FARM_SHIFT):
		raise ToolError(
			f"this site has no {FARM_SHIFT} DocType, so {name!r} cannot be linked. It ships with "
			"erpnext_mcp — run `bench --site <site> migrate`. Nothing was changed."
		)
	if not frappe.db.exists(FARM_SHIFT, name):
		raise ToolError(
			f"no {FARM_SHIFT} called {name!r} on this site. list_shifts has the register; a docname "
			"looks like SHIFT-2026-0001. Nothing was changed."
		)
	if company:
		theirs = str(frappe.db.get_value(FARM_SHIFT, name, "company") or "")
		if theirs and theirs != company:
			raise ToolError(
				f"{name} is a shift at {theirs} and this task is at {company}. A completion filed "
				"against another entity's shift puts one company's evidence on another company's "
				"compliance record, which is the one direction an auditor cannot unpick. Nothing "
				"was changed."
			)
	return name


def _open_shift_for(worker: str, company: str = "") -> str:
	"""The ONE open shift this worker is rostered on right now, or "".

	v0.64.0. AMBIGUITY RETURNS NOTHING. Two open shifts naming the same person is
	a roster somebody needs to fix, and picking the newer of them would put a
	completion's evidence on a crew that was somewhere else — which is a false
	entry on a compliance record rather than a missing one, and the two are not
	equally bad. Nobody is rostered on two crews at once in practice; when the
	data says otherwise, the data is what has to be believed.

	NEVER RAISES. This is a convenience on a clock-in, and a phone that could not
	be told which shift it is on should still be able to start the job.
	"""
	if not worker or not compat.doctype_exists(FARM_SHIFT):
		return ""
	try:
		filters = {"employee": worker, "left_at": ("is", "not set"), "parenttype": FARM_SHIFT}
		rows = frappe.db.get_all("Farm Shift Crew Member", filters=filters, fields=["parent"], limit=50)
		names = sorted({str(entry.get("parent") or "") for entry in rows or [] if entry.get("parent")})
		if not names:
			return ""
		shift_filters = {"name": ("in", names), "end_datetime": ("is", "not set")}
		if company:
			shift_filters["company"] = company
		open_now = frappe.db.get_all(FARM_SHIFT, filters=shift_filters, fields=["name"], limit=5)
		found = sorted({str(entry["name"]) for entry in open_now or []})
		return found[0] if len(found) == 1 else ""
	except Exception:
		return ""


def _rules_this_completion_answers(task: dict, produced_doctype: str) -> list:
	"""The alert types worth re-running now that this work is filed.

	v0.64.0. TWO SOURCES, BOTH NARROW ON PURPOSE.

	The first is the rule that RAISED this task. If a compliance alert sent
	somebody to walk a cabin, the rule behind that alert is by definition the one
	whose condition the walk was meant to change, and it is the alert the worker
	is watching.

	The second is every rule whose target doctype is the record this completion
	PRODUCED. A spray task that files a pesticide application record, a detector
	round that files an inspection, a water task that files a test — the rule that
	asks "when was the last one" reads exactly that register, and it is the rule
	an EPA or FSMA question turns on. This is the half that makes a task
	compliance-native rather than merely compliance-adjacent: nobody had to link
	the task to the rule, because the RECORD is the link.

	────────────────────────────────────────────────────────────────────────
	v0.64.1: `requires` IS NOT THE REGISTER, AND ON THE THREE RULES THAT
	MATTER MOST IT IS NOT EVEN CLOSE
	────────────────────────────────────────────────────────────────────────

	The second half above was written as "`produced_doctype` in `rule.requires`"
	and that misses exactly the rules a completion is trying to answer. A
	built-in scanner declares `requires` as the doctype it SCANS, not the
	register it reads through:

	    housing_inspection_overdue   scans Housing Unit, produced Housing Inspection
	    housing_detector_test_stale  scans Housing Unit, produced Detector Test
	    water_test_stale             scans Field,        produced Water Test

	None of the three matched. What DID match were the rules that read the
	produced register to raise a NEW problem — `housing_corrective_action_open`,
	`water_test_contamination` — so a habitability walk re-ran the rule that
	opens a finding against it and never the rule whose alert sent somebody to
	walk the cabin. The alert stood until the hourly sweep, on the phone, in
	front of the worker who had just done the work: precisely the failure the
	narrowed sweep was added to end.

	Those rules go through the register by WRITE-BACK — recording an inspection
	moves `Housing Unit.last_habitability_inspection`, which is what makes the
	condition false (`records.py` argues why that write-back is in the
	controller). So the register a rule answers to is not derivable from
	`requires`, and `ALERT_TASK_MAP` is where this app already states it: each
	recipe names the record the alert's own work produces. Read backwards, it is
	the map from a produced record to the alerts that record answers — and it is
	the same table the task was built from, so the two cannot drift.

	BOTH TESTS ARE KEPT, because they are true of different rules.
	`water_test_contamination` genuinely requires Water Test and is genuinely
	worth re-running when one is filed; it just is not the rule the task was
	raised by.

	IT RETURNS NAMES AND EVALUATES NOTHING. Deciding what is relevant and
	deciding what is true are separate jobs, and the second belongs to the rule.
	"""
	wanted = []
	alert = str(task.get("source_alert") or "")
	if alert and compat.doctype_exists(ALERT):
		try:
			alert_type = str(frappe.db.get_value(ALERT, alert, "alert_type") or "")
			if alert_type:
				wanted.append(alert_type)
		except Exception:
			pass
	if produced_doctype:
		wanted.extend(_rules_reading_register(produced_doctype))
		try:
			from ..alerts import base as alerts_base

			rules, _notes = alerts_base.resolve_rules()
			for key, rule in (rules or {}).items():
				# `requires` is the doctypes a rule cannot run without, which is
				# where a declarative rule's target_doctype lands. A rule that
				# cannot be run without the register this completion just wrote to
				# is a rule whose answer may have just changed.
				if produced_doctype in tuple(rule.requires or ()):
					wanted.append(key)
		except Exception:
			pass
	return sorted(set(wanted))


def _rules_reading_register(produced_doctype: str) -> list:
	"""The alert types whose own work produces `produced_doctype`. Never raises.

	`ALERT_TASK_MAP` READ BACKWARDS, and that is the whole of it. The table says
	what each alert becomes when it stops being a warning and starts being work,
	and `creates_record` on each recipe is the register that alert's condition is
	discharged through. A completion that just wrote to that register is a
	completion whose answer those rules may have changed — whether or not this
	particular task was ever raised from an alert, which is the case the
	`source_alert` half cannot reach.

	NAMES ONLY, AND UNFILTERED BY WHAT THIS SITE HAS. `refresh_compliance_alerts`
	already reports an `alert_types` entry it does not recognise as skipped rather
	than failing on it — a rule renamed or disabled since is an ordinary state of
	a live site and must not be able to fail a completion already filed.
	"""
	wanted = str(produced_doctype or "").strip()
	if not wanted:
		return []
	return sorted(
		alert_type
		for alert_type, recipe in ALERT_TASK_MAP.items()
		if str((recipe or {}).get("creates_record") or "").strip() == wanted
	)


def _evaluate_compliance_after(task: dict, produced_doctype: str, company: str) -> dict | None:
	"""Re-run the rules this completion could have changed. Never raises.

	THIS IS THE SWEEP, CALLED SOONER — not a shortcut around it. Nothing here
	dismisses an alert because a task was completed; it re-runs the rule and lets
	the rule's own condition decide, exactly as the nightly run would. A task
	completed against a condition that is still true leaves its alert standing,
	which is the outcome that matters most: doing the work and fixing the problem
	are two different facts and both have to be reportable.

	NEVER RAISES AND NEVER FAILS THE COMPLETION. The work is filed and the
	evidence is on the record before this runs. A rule that throws, a site
	mid-migrate, a register this app cannot read — none of them is a reason to
	turn a worker's filed completion into an error on their phone.
	"""
	names = _rules_this_completion_answers(task, produced_doctype)
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
				f"the narrowed sweep did not run: {type(exc).__name__}: {exc}. The completion, its "
				"evidence and its produced record are unaffected, and the scheduled sweep will "
				"reach these rules on its next pass."
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
			"These are the rules whose answer this completion could have changed — the one that "
			"raised the task, and any that read the register it wrote to. THE SWEEP DECIDED, not "
			"the completion: an alert here went away because its rule looked again and found its "
			"condition no longer true, which is the only honest way one goes away."
		),
	}
	if not dismissed:
		out["standing_note"] = (
			"No alert dismissed. That is a normal and often correct outcome: the work is done and "
			"the condition that raised the alert may still be true — a cabin walked and found "
			"faulty is a completed task and an open problem, and both facts survive this call."
		)
	return out


#: The two rules a finished spray can raise on the spot. Named here rather than
#: derived, for the reason `_rules_this_completion_answers` gives about every
#: other narrowing in this file: a sweep that guessed which rules a completion
#: could have changed would either run all of them on every tap or quietly miss
#: the one the worker is watching.
SPRAY_WINDOW_RULES = ("rei_active_block_entry", "phi_harvest_window")


def _evaluate_spray_windows(windows: dict, company: str) -> dict | None:
	"""Raise the REI and PHI alerts for a spray that just finished. Never raises.

	SEPARATE FROM `_evaluate_compliance_after` AND DELIBERATELY AFTER IT. That one
	runs before the task's own state is written, because what it re-asks are rules
	about the REGISTER a completion wrote to. These two read columns that only
	exist once `_set_task_state` has stamped them, so they have to come after —
	and folding them into one call would mean reordering a sweep that a dozen
	tests and one iOS build already depend on the timing of.

	Returns None where this completion opened no window at all, which is every
	non-spray task and every spray of nothing restricted. Saying "no window" is
	different from saying nothing.
	"""
	if not (windows.get("rei_expires_at") or windows.get("phi_clears_on")):
		return None
	try:
		from ..alerts import base as alerts_base

		report = alerts_base.refresh_compliance_alerts(
			company=company or "", alert_types=list(SPRAY_WINDOW_RULES)
		)
	except Exception as exc:
		return {
			"rules_asked": list(SPRAY_WINDOW_RULES),
			"evaluated": False,
			"why_not": (
				f"the interval rules did not run: {type(exc).__name__}: {exc}. The window is "
				"STAMPED ON THE TASK either way — the block's expiry is recorded and the "
				"scheduled sweep will raise the alert on its next pass."
			),
		}
	return {
		"rules_asked": list(SPRAY_WINDOW_RULES),
		"evaluated": True,
		"created": report.get("created", 0),
		"refreshed": report.get("refreshed", 0),
		"note": (
			"The restricted-entry and pre-harvest windows this application opened are now on the "
			"compliance calendar, scoped to the block. Neither is dismissible by hand and neither "
			"needs to be: each one silences itself the moment its own interval closes."
		),
	}


def _shift_carries_event(shift: str, assignment: str) -> bool:
	"""Does this shift's timeline already carry an event produced by `assignment`?

	Read-only, and never raises: it answers a reporting question on a replay
	path whose entire promise is that it writes nothing.
	"""
	if not shift or not assignment or not compat.doctype_exists(FARM_SHIFT):
		return False
	try:
		from .. import shifts as shifts_mod

		return any(
			str(entry.get("producer_record_name") or "") == assignment
			for entry in shifts_mod.events_of(shift)
		)
	except Exception:
		return False


def _weather_at(shift: str, when: str) -> dict:
	"""The shift's last weather reading AT OR BEFORE `when`. Never the next one.

	v0.64.0. AT OR BEFORE, and the asymmetry is the whole of it. A completion at
	11:52 sits between the 11:45 and 12:00 readings, and the honest snapshot is
	the one that had already been taken when the work finished — reaching forward
	to 12:00 would stamp a record with a measurement that did not exist yet, which
	is the kind of detail that turns a good-faith record into a disputed one.

	Empty where the shift has no timeline, which is not the same as zero and is
	why the caller writes nothing rather than writing nulls.
	"""
	if not when:
		return {}
	try:
		from .. import shifts as shifts_mod

		before = [
			entry
			for entry in shifts_mod.weather_of(shift)
			if str(entry.get("reading_datetime") or "") and str(entry["reading_datetime"]) <= when
		]
	except Exception:
		return {}
	return dict(before[-1]) if before else {}


def _flow_evidence_into_shift(assignment_doc, task: dict, evidence: list, clean_pass) -> dict | None:
	"""Append this completion to its shift's compliance timeline. Never raises.

	v0.64.0. THIS IS THE JOIN THE WHOLE TASK↔SHIFT LINK EXISTS FOR. A task
	completion carries a point in time: this cabin, this worker, these two
	photographs, 11:52. A shift carries the period an exposure regime asks about.
	Until this function ran, the two records could both be perfect and still leave
	nobody able to answer "what was done during the July 15 shift" without
	reconciling two registers by hand — which is the reconciliation that does not
	happen on the afternoon an inspector asks for it.

	WHAT IS COPIED AND WHAT IS DELIBERATELY NOT. The event carries the timestamp,
	the worker who filed it, a sentence naming the work and its outcome, the
	signature file, and the weather AS IT STOOD AT OR BEFORE the completion. It
	does NOT copy the photographs onto the shift: the evidence lives on the
	assignment, the event names the assignment in `producer_record_name`, and a
	second copy of a photograph is a second thing that can drift from the first.
	The event is a POINTER WITH ENOUGH ON IT TO BE READ WITHOUT FOLLOWING.

	`logged_by` IS WRITTEN HERE AND IS LEFT EMPTY BY THE WEATHER SWEEP, and the
	difference is not an inconsistency. Somebody DID this work and signed for it;
	naming them is the record being true. Nobody observed a temperature — the
	sweep did — and naming the foreman against a reading they did not take would
	put their identity behind an observation they never made.

	IT NEVER RAISES AND IT NEVER FAILS THE COMPLETION. The completion is already
	saved and its evidence is already filed when this runs. A shift deleted out
	from under a task, a site mid-migrate, a foreman who closed the shift a second
	ago — none of those is a reason to throw away a worker's filed evidence, so
	every failure comes back as a sentence the caller reports.
	"""
	shift = str(assignment_doc.get("farm_shift") or "")
	if not shift:
		return None
	if not compat.doctype_exists(FARM_SHIFT) or not frappe.db.exists(FARM_SHIFT, shift):
		return {
			"farm_shift": shift,
			"event_logged": False,
			"why_not": (
				f"{shift} is no longer on this site, so there is no compliance timeline to append "
				"to. The completion, its evidence and its produced record are unaffected — they "
				"are on the assignment, which is where they have always been."
			),
		}

	when = str(assignment_doc.get("completed_at") or "") or frappe.utils.now()

	# ONE EVENT PER ASSIGNMENT. `complete_farm_task` already refuses a second
	# completion and absorbs an idempotent replay before reaching here, but a
	# shift is edited in the Desk too — and two identical entries on a timeline
	# is the failure `already_crossed` exists to prevent one doctype over.
	try:
		from .. import shifts as shifts_mod

		for entry in shifts_mod.events_of(shift):
			if str(entry.get("producer_record_name") or "") == assignment_doc.name:
				return {
					"farm_shift": shift,
					"event_logged": False,
					"why_not": (
						f"{shift} already carries an event produced by {assignment_doc.name}. One "
						"completion is one entry on a timeline; a second would be the same work "
						"appearing to have been done twice."
					),
				}
	except Exception:
		pass

	reading = _weather_at(shift, when)
	outcome = (
		"no issues found"
		if clean_pass is True
		else (
			f"findings recorded: {str(assignment_doc.get('findings_text') or '').strip()[:200]}"
			if str(assignment_doc.get("findings_text") or "").strip()
			else "completed"
		)
	)
	description = (
		f"{task.get('task_type') or 'Task'} '{task.get('task_name') or assignment_doc.task}' "
		f"({assignment_doc.task}) completed by "
		f"{assignment_doc.get('assigned_to_name') or assignment_doc.get('assigned_to')} — {outcome}. "
		f"{len(evidence)} evidence file(s) and the completion signature are on "
		f"{assignment_doc.name}, which is the record of what was produced; this entry is where it "
		"sits on the shift's own timeline."
	)
	if task.get("produced_record") or assignment_doc.get("produced_record"):
		description += (
			f" Produced {task.get('creates_record') or 'record'} "
			f"{assignment_doc.get('produced_record') or task.get('produced_record')}."
		)

	row = {
		"event_type": TASK_COMPLETED_EVENT,
		"event_datetime": when,
		"description": description,
		"producer_record_doctype": FARM_TASK_ASSIGNMENT,
		"producer_record_name": assignment_doc.name,
	}
	# The signature is the attested part of the completion, so it is the file
	# worth carrying onto a record somebody else will read. Written only when
	# there is one — a blank Attach is not evidence of anything.
	if assignment_doc.get("signature_file"):
		row["evidence_file"] = assignment_doc.signature_file
	# `logged_by` is a Link to Employee. A worker id that is not an Employee on
	# this site (a badge-only site, an archived hire) leaves the column empty
	# rather than refusing — the name is already in the description.
	worker = str(assignment_doc.get("assigned_to") or "")
	if worker and hr_installed() and frappe.db.exists(EMPLOYEE, worker):
		row["logged_by"] = worker
	if reading.get("temp_f") is not None:
		row["weather_snapshot_temp_f"] = reading.get("temp_f")
	if reading.get("heat_index_f") is not None:
		row["weather_snapshot_heat_index_f"] = reading.get("heat_index_f")

	try:
		shift_doc = frappe.get_doc(FARM_SHIFT, shift)
		shift_doc.append("compliance_events", row)
		shift_doc.flags.ignore_permissions = True
		shift_doc.save(ignore_permissions=True)
	except Exception as exc:
		return {
			"farm_shift": shift,
			"event_logged": False,
			"why_not": (
				f"could not append to {shift}'s timeline: {type(exc).__name__}: {exc}. The "
				"completion, its evidence and its produced record are unaffected — a shift that "
				"will not take an entry is not a reason to throw away work somebody has signed for."
			),
		}

	out = {
		"farm_shift": shift,
		"event_logged": True,
		"event_type": TASK_COMPLETED_EVENT,
		"event_datetime": when,
		"weather_snapshot_temp_f": row.get("weather_snapshot_temp_f"),
		"weather_snapshot_heat_index_f": row.get("weather_snapshot_heat_index_f"),
		"evidence_file_carried": bool(row.get("evidence_file")),
		"note": (
			f"This completion is now on {shift}'s compliance timeline, beside the water breaks, "
			"the observations and the weather readings for the same afternoon. That is what makes "
			"'what was done during this shift' one read instead of a reconciliation between two "
			"registers — and the reconciliation is what does not happen on the day somebody asks."
		),
	}
	if not reading:
		out["weather_note"] = (
			f"No weather reading had been taken on {shift} at or before {when}, so the event "
			"carries no snapshot. Null rather than zero: nobody measured, which is not a "
			"temperature."
		)
	return out


def _shift_note(shift: str) -> str:
	"""The sentence a closed or cancelled shift earns on a task that names it."""
	if not shift or not compat.doctype_exists(FARM_SHIFT):
		return ""
	row = dict(
		frappe.db.get_value(FARM_SHIFT, shift, ["status", "end_datetime", "cancelled"], as_dict=True) or {}
	)
	if compat.checked(row.get("cancelled")):
		return (
			f"{shift} is CANCELLED. The link is kept because it is still the true answer to which "
			"shift this work was raised for, but a cancelled shift's compliance record is not "
			"evidence of anything and nothing will flow onto it."
		)
	if row.get("end_datetime"):
		return (
			f"{shift} is already closed (ended {row['end_datetime']}). The link is kept and a "
			"completion will still append its event to the shift's timeline — an event logged "
			"after the close is a late entry rather than a false one, and the timestamps say "
			"which. What it cannot do is change a supervisor's signature that has already been "
			"given."
		)
	return ""


def _describe_task(row: dict) -> dict:
	contract = evidence_contract(row.get("evidence_required"))
	out = {
		"name": row.get("name"),
		"task_name": row.get("task_name"),
		"task_type": row.get("task_type"),
		"state": row.get("state") or DRAFT,
		"urgency": row.get("urgency") or "Normal",
		"origin": row.get("origin") or ORIGIN_COMPLIANCE_RULE,
		"company": row.get("company") or None,
		"location_doctype": row.get("location_doctype") or None,
		"location": row.get("location") or None,
		"skill_required": row.get("skill_required") or None,
		"dispatch_mode": row.get("dispatch_mode") or "Either",
		"assigned_to": row.get("assigned_to") or None,
		"assigned_to_name": row.get("assigned_to_name") or row.get("assigned_to") or None,
		"estimated_duration_minutes": int(row.get("estimated_duration_minutes") or 0) or None,
		"source_alert": row.get("source_alert") or None,
		"source_workorder": row.get("source_workorder") or None,
		"evidence_required": contract,
		"evidence_required_summary": _contract_sentence(contract),
		"creates_record": row.get("creates_record") or None,
		"creates_record_data": _safe_json(row.get("creates_record_data")),
		"produced_record": row.get("produced_record") or None,
		"notes": row.get("notes") or None,
		"open": (row.get("state") or DRAFT) not in TERMINAL_STATES,
		"self_pickable": (row.get("dispatch_mode") or "Either") in SELF_PICKABLE,
	}
	# v0.41.0. The template is PROVENANCE and the checklist is the task's own
	# snapshot — both reported only when there is one, so the shape of a plain
	# hand-raised task's payload is exactly what it was before this release.
	if row.get("template"):
		out["template"] = row["template"]
	items = checklist_items(row.get("checklist_status"))
	if items:
		out["checklist"] = items
		outstanding = unmet_checklist(row.get("checklist_status"))
		out["checklist_done"] = len([item for item in items if item.get("done")])
		out["checklist_outstanding_required"] = outstanding
	if row.get("asset"):
		out["asset"] = row["asset"]
	if row.get("reported_by"):
		out["reported_by"] = row["reported_by"]
	if row.get("reported_at"):
		out["reported_at"] = str(row["reported_at"])
	# v0.98.0, item 5. WHEN IT WAS SEEN, where that is not when it was filed.
	# Present only where somebody said so, on the same rule as every key around
	# it — a task nobody gave an observation time for reports none rather than
	# echoing `reported_at` and inventing a precision that was never claimed.
	if row.get("observed_at"):
		out["observed_at"] = str(row["observed_at"])
	if row.get("report_photo"):
		out["report_photo"] = row["report_photo"]
	# v0.64.0. Reported only when there is one, so the payload of a task raised
	# before shifts existed is exactly the shape it has always been.
	if row.get("farm_shift"):
		out["farm_shift"] = row["farm_shift"]
	# v0.69.0, on the same rule: present only where the task has one, so nothing
	# about a habitability walk's payload changed. The tank mix is what a phone
	# shows an applicator BEFORE they fill the sprayer, and the two windows are
	# what it shows anybody else who opens the block's last spray afterwards.
	materials = _quiet_materials(row.get("materials_used"))
	if materials:
		out["materials_used"] = materials
	for column in ("spray_completed_at", "rei_expires_at", "phi_clears_on"):
		if row.get(column):
			out[column] = str(row[column])
	for column in ("rei_source_item", "phi_source_item"):
		if row.get(column):
			out[column] = row[column]
	return out


def _safe_json(raw) -> dict:
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
	except Exception:
		return {}
	return value if isinstance(value, dict) else {}


def _contract_sentence(contract: dict) -> str:
	wanted = [key for key, value in sorted(contract.items()) if value]
	if not wanted:
		return "nothing (which should not be possible — evidence_required is mandatory at creation)"
	return "; ".join(f"{key}: {EVIDENCE_KEYS[key]}" for key in wanted if key in EVIDENCE_KEYS)


def _describe_assignment(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"task": row.get("task"),
		"task_name": row.get("task_name"),
		"assigned_to": row.get("assigned_to"),
		"assigned_to_name": row.get("assigned_to_name") or row.get("assigned_to"),
		"state": row.get("state"),
		"company": row.get("company") or None,
		"dispatched_by_foreman": compat.checked(row.get("dispatched_by_foreman")),
		"claimed_at": str(row.get("claimed_at") or "") or None,
		"started_at": str(row.get("started_at") or "") or None,
		"completed_at": str(row.get("completed_at") or "") or None,
		"actual_duration_minutes": int(row.get("actual_duration_minutes") or 0) or None,
		"completion_narrative": row.get("completion_narrative") or None,
		"findings_text": row.get("findings_text") or None,
		"witness": row.get("witness") or None,
		"farm_location_gps": row.get("farm_location_gps") or None,
		"rejection_reason": row.get("rejection_reason") or None,
		"signature_file": row.get("signature_file") or None,
		"produced_record": row.get("produced_record") or None,
		# v0.20.1. `visit_id` is reported so a client can PROVE THE ROUND TRIP —
		# the handset mints it, and reading it back off the record is the only
		# way the app can tell "the server has my visit" from "the server has my
		# completion and dropped the grouping". The signature is reported once at
		# the TOP LEVEL of a completion's result rather than on every assignment
		# any tool happens to describe: it is a fact about one submission, not a
		# property of the assignment worth repeating on a dispatch board.
		"visit_id": row.get("visit_id") or None,
		# v0.64.0. WHICH SHIFT THE WORK WAS ACTUALLY DONE ON, which is not always
		# the shift the task was raised for. Always present rather than
		# conditional — a client that has to test for the key before reading it
		# has two paths where it needs one, and this one decides whether a
		# completion's evidence reached a compliance record at all.
		"farm_shift": row.get("farm_shift") or None,
	}


def _worker(args: dict, key: str = "worker_id", required: bool = True) -> str:
	"""A worker id, validated against the Employee register where there is one."""
	worker = as_str(args, key) or as_str(args, "assigned_to") or as_str(args, "employee")
	if not worker:
		if required:
			raise ToolError(
				f"{key} is required. A dispatch record with no name on it answers none of the "
				"questions it exists to answer."
			)
		return ""
	if hr_installed() and not frappe.db.exists(EMPLOYEE, worker):
		raise ToolError(
			f"no Employee {worker!r} on this site. A dispatch board naming somebody payroll has "
			"never heard of has already drifted from the operation it describes. list_employees "
			"has the register. Nothing was changed."
		)
	return worker


def _refuse_a_minor_on_prohibited_work(
	worker: str, worker_name: str, task: dict, verb: str = "changed"
) -> dict:
	"""Refuse to send somebody under eighteen to work an age bar closes to them.

	v0.98.0. AN AGE BAR AND NOT A TRAINING GAP, which is the whole reason this is
	a refusal in a file whose posture is otherwise to record and report. Every
	other warning `assign_farm_task` raises — a live re-entry interval, a
	concurrent claim, a missing I-9 — describes work that is lawful with the right
	precaution, so refusing would invent a rule stricter than the regulation and
	teach a foreman to route around this app. 40 CFR §170.309(c) and 29 CFR
	§570.71(a) are not like that: there is no course, no PPE and no supervision
	that makes a sixteen-year-old a lawful pesticide handler.

	Returns the minor findings on a dispatch that is allowed, so the caller can
	put "this person is seventeen" on the answer. Empty for an adult, and empty
	with a NOTE where no date of birth is recorded — never a refusal on a blank
	column, for the same reason `minor_findings` gives.
	"""
	if not (worker and hr_installed() and compat.has_field(EMPLOYEE, "date_of_birth")):
		return {}
	born = frappe.db.get_value(EMPLOYEE, worker, "date_of_birth")
	described = minors.describe(born, frappe.utils.today())
	if described["is_minor"] is None:
		return {
			"date_of_birth_missing": (
				f"{worker_name or worker} has no date of birth on file, so this app cannot check "
				"the age bars on this kind of work. Set it with update_employee."
			)
		}
	if not described["is_minor"]:
		return {}
	reason = minors.prohibited_reason(described["minor_band"], str(task.get("task_type") or ""))
	if reason:
		raise ToolError(
			f"{worker_name or worker} is {described['age']}, and {task.get('name')} is a "
			f"{task.get('task_type')} task. {reason} Nothing was {verb}. Send somebody eighteen "
			"or over, or raise a different task."
		)
	return described


def _worker_name(worker: str, given: str = "") -> str:
	if given:
		return given
	if worker and compat.doctype_exists(EMPLOYEE):
		return str(frappe.db.get_value(EMPLOYEE, worker, "employee_name") or "") or worker
	return worker


def _assignment_row(name: str) -> dict:
	return dict(
		frappe.db.get_value(
			FARM_TASK_ASSIGNMENT,
			name,
			compat.existing_fields(FARM_TASK_ASSIGNMENT, _ASSIGNMENT_FIELDS),
			as_dict=True,
		)
		or {}
	)


def _last_completion(task: str) -> str:
	"""The most recent Completed assignment on this task, or "".

	v0.20.1, AND ONLY THE COMPLETION PATH ASKS FOR IT. A retry that names just
	the task finds no LIVE assignment, because the completion it is retrying is
	what ended the live one — so without this the second attempt is refused with
	"nobody is holding it", which is a worse answer than "already completed" and
	the same lost work. Newest first, because a task claimed, completed, reopened
	and completed again has two, and the one a client is retrying is the last.
	"""
	rows = (
		frappe.db.get_all(
			FARM_TASK_ASSIGNMENT,
			filters={"task": task, "state": COMPLETED},
			pluck="name",
			order_by="modified desc",
			limit=1,
		)
		or []
	)
	return str(rows[0]) if rows else ""


def _assignment_for(args: dict, task_name: str = "", or_completed: bool = False) -> dict:
	"""The live assignment named by `assignment`, or inferred from the task.

	`or_completed` falls back to the newest finished assignment where nothing is
	live. ONLY `complete_farm_task` passes it, and only so that a re-sent
	completion reaches the signature check instead of a refusal about an empty
	board — starting or rejecting a task that is already finished is a genuine
	mistake and still says so.
	"""
	explicit = as_str(args, "assignment") or as_str(args, "task_assignment")
	if explicit:
		if not frappe.db.exists(FARM_TASK_ASSIGNMENT, explicit):
			raise ToolError(f"no Farm Task Assignment called {explicit!r} on this site. Nothing was changed.")
		return _assignment_row(explicit)

	task = task_name or as_str(args, "task", required=True)
	name = live_assignment(task) or (_last_completion(task) if or_completed else "")
	if not name:
		raise ToolError(
			f"{task} has nobody holding it, so there is nothing to start, complete or reject. It has "
			"to be claimed with claim_farm_task or dispatched with assign_farm_task first. Nothing "
			"was changed."
		)
	return _assignment_row(name)


def _set_task_state(task: str, state: str, **fields) -> None:
	doc = frappe.get_doc(FARM_TASK, task)
	doc.state = state
	for key, value in fields.items():
		doc.set(key, value)
	doc.save(ignore_permissions=True)


# ── 1. create_farm_task ─────────────────────────────────────────────────────
#: The block register a bare `affected_block` is resolved against. `Field` is
#: what a farm calls a block; the other three registers are named explicitly by
#: the caller through `location_doctype` and are not guessed at from this key.
AFFECTED_BLOCK_DOCTYPE = "Field"


def _structured_report(args: dict, doc, location_doctype: str, location: str) -> tuple:
	"""The four facts an ad-hoc task used to carry as prose. v0.98.0, item 5.

	`SERVER_CHANGES.md` §5: everything below the first line of a handset's
	`description` is composed by the app and stored by the server as one blob, so
	the affected asset, the affected block, the time it was seen and the person
	who saw it were greppable and not queryable. Each of these four lands in a
	COLUMN THAT ALREADY EXISTS — `asset`, the `location_doctype`/`location` pair,
	`observed_at` (new in v0.98.0, and the only new one) and `reported_by` — so
	nothing is stored twice and every report already written keeps its shape.

	Returns the `(location_doctype, location)` pair the caller should use, which
	may have been filled in from `affected_block`.

	`affected_block` ONLY FILLS A LOCATION THAT IS OTHERWISE EMPTY. A body that
	names both a location pair and a block is a body saying two things about one
	column, and the explicit pair wins because it carries its own register — but
	it is refused rather than silently preferred where the two disagree, since a
	task routed to the wrong ground is a crew sent to the wrong ground.

	`reported_by` IS NOT SET HERE FOR A FIELD REPORT. That caller's reporter is
	the authenticated worker and is resolved before this runs; letting a body
	name somebody else would put a stranger's name on a report they never made.
	"""
	block = as_str(args, "affected_block")
	if block:
		if location and location != block:
			raise ToolError(
				f"affected_block is {block!r} and location is {location!r}, which are two "
				"answers to where this work is. Send one. Nothing was created."
			)
		if not location:
			if not frappe.db.exists(AFFECTED_BLOCK_DOCTYPE, block):
				raise ToolError(
					f"no {AFFECTED_BLOCK_DOCTYPE} called {block!r} on this site. affected_block is "
					f"resolved against the {AFFECTED_BLOCK_DOCTYPE} register; for a parcel, a zone "
					"or a cabin send location_doctype and location instead. Nothing was created."
				)
			location_doctype, location = AFFECTED_BLOCK_DOCTYPE, block

	asset_name = as_str(args, "affected_asset") or as_str(args, "asset")
	if asset_name and compat.has_field(FARM_TASK, "asset"):
		from .asset_tags import asset_row

		doc.asset = asset_row(asset_name)["name"]

	seen = as_str(args, "observed_at")
	if seen and compat.has_field(FARM_TASK, "observed_at"):
		# NEVER `reported_at`, which is the filing stamp `_field_report_count`
		# counts the five-per-hour limit on. A caller who could move that column
		# backwards could file a hundred reports dated an hour ago and the
		# anti-spam rule would count none of them.
		#
		# THROUGH `as_mariadb_datetime` BECAUSE THE SENDER IS AN IPHONE.
		# `ISO8601DateFormatter` writes `2026-08-18T07:12:00Z`, and a Frappe
		# Datetime column answers that with `Incorrect datetime value` at the
		# insert — the same wall the whole bucket capture queue hit in v0.59.1,
		# and the reason that conversion is its own module. Unreadable is refused
		# HERE, by name, rather than left to surface as a framework error from
		# `doc.insert()` with a task half built behind it.
		stamp = datetimes.as_mariadb_datetime(seen)
		if not stamp:
			raise ToolError(
				f"observed_at {seen!r} is not a timestamp this can read. Send ISO 8601 — "
				"'2026-08-18T07:12:00Z' or '2026-08-18 07:12:00'. Nothing was created."
			)
		doc.observed_at = stamp

	return location_doctype, location


def create_farm_task(args: dict) -> ToolResult:
	"""Raise one piece of work, with the evidence closing it requires stated up front."""
	_require()
	company = _company(args)

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
				f"this site has no {location_doctype!r} DocType, so {location!r} cannot be resolved. "
				"Nothing was created."
			)
		if not frappe.db.exists(location_doctype, location):
			raise ToolError(
				f"no {location_doctype} called {location!r} on this site. A task about a place that "
				"does not exist cannot be routed to anybody stood in it. Nothing was created."
			)

	creates_record = as_str(args, "creates_record")
	if creates_record and not compat.doctype_exists(creates_record):
		raise ToolError(
			f"this task promises to produce a {creates_record!r} and this site does not have that "
			"DocType. A task promising a record nobody can write is a promise that fails in front "
			f"of a worker stood in a cabin. Known builders: {', '.join(sorted(inspections.BUILDERS))}. "
			"Nothing was created."
		)

	source_alert = as_str(args, "source_alert")
	if source_alert:
		if not compat.doctype_exists(ALERT) or not frappe.db.exists(ALERT, source_alert):
			raise ToolError(f"no Compliance Alert called {source_alert!r} on this site. Nothing was created.")
		existing = frappe.db.get_value(FARM_TASK, {"source_alert": source_alert}, "name")
		if existing:
			raise ToolError(
				f"Farm Task {existing} already answers alert {source_alert}. One task per alert — "
				"two people sent to walk the same cabin is the thing the source link exists to "
				"prevent. Nothing was created."
			)

	worker = _worker(args, "assigned_to", required=False)
	draft = as_bool(args, "draft", False)
	farm_shift = _shift_argument(args, company or "")

	# THE PRE-HARVEST GUARD, RUN BEFORE ANYTHING IS INSERTED. Raising a Harvest
	# task on a block is the moment a pick is planned, and it is the last moment
	# the plan can be changed for free — a block moved back now costs a sentence,
	# and the same decision discovered at the packing house costs a load. See
	# `_refuse_harvest_inside_phi` for why this refuses where the REI below only
	# warns. Read off the ARGUMENTS rather than the document, so nothing exists
	# to roll back when it refuses.
	phi_override = _refuse_harvest_inside_phi(
		{
			"task_type": as_str(args, "task_type"),
			"location": location,
			"asset": "",
			"company": company or "",
		},
		args,
		"created",
		override_tool="create_farm_task",
	)

	doc = frappe.new_doc(FARM_TASK)
	doc.task_name = as_str(args, "task_name", required=True)
	doc.task_type = as_choice(FARM_TASK, "task_type", as_str(args, "task_type", required=True), "task_type")
	doc.urgency = as_choice(FARM_TASK, "urgency", as_str(args, "urgency") or "Normal", "urgency")
	doc.dispatch_mode = as_choice(
		FARM_TASK, "dispatch_mode", as_str(args, "dispatch_mode") or "Either", "dispatch_mode"
	)
	doc.company = company
	doc.location_doctype = location_doctype or None
	doc.location = location or None
	doc.skill_required = as_str(args, "skill_required")
	doc.estimated_duration_minutes = as_int(args, "estimated_duration_minutes") or 0
	doc.source_alert = source_alert or None
	doc.source_workorder = as_str(args, "source_workorder")
	doc.creates_record = creates_record
	doc.creates_record_data = json.dumps(
		parse_json_object(args.get("creates_record_data"), "creates_record_data")
	)
	doc.notes = _with_override_note(as_str(args, "notes"), phi_override)
	# v0.98.0, item 5. The four structured facts, into the four columns that hold
	# them. `_structured_report` may fill the location pair in from
	# `affected_block`, which is why its answer is assigned back — a task raised
	# with only a block name still gets routed to ground somebody is stood on.
	#
	# `reported_by` IS ACCEPTED HERE AND NOT ON A FIELD REPORT, and the two are
	# different questions. This door is a FOREMAN raising work about something
	# somebody else told them, so recording who saw it is the whole point; there
	# the reporter is the authenticated worker and a body naming anybody else
	# would be putting a stranger's name on their own report.
	location_doctype, location = _structured_report(args, doc, location_doctype, location)
	doc.location_doctype = location_doctype or None
	doc.location = location or None
	observer = _worker(args, "reported_by", required=False)
	if observer:
		doc.reported_by = observer
		doc.reported_at = frappe.utils.now()
	# v0.79.0. A step of a longer piece of work — see the multi-day block below.
	parent_doctype, parent = _parent_argument(args, "created")
	if parent:
		doc.parent_doctype = parent_doctype
		doc.parent_task = parent
	# v0.69.0. THE TANK MIX, STATED WHEN THE WORK IS RAISED. Refused here rather
	# than absorbed: a spray task is dispatched with the mix on it precisely so
	# the applicator is not deciding at the sprayer, and a malformed list is a
	# planning error somebody can fix now, before anybody drives anywhere.
	# `complete_farm_task` draws whatever is on this column down out of stock.
	doc.materials_used = json.dumps(_materials_argument(args))
	doc.evidence_required = json.dumps(_evidence_argument(args))
	doc.farm_shift = farm_shift or None
	doc.state = DRAFT if draft else AVAILABLE
	if worker:
		doc.assigned_to = worker
		doc.assigned_to_name = _worker_name(worker, as_str(args, "assigned_to_name"))
		doc.state = DRAFT if draft else CLAIMED
	doc.insert(ignore_permissions=True)

	assignment = None
	if worker and not draft:
		assignment = _open_assignment(doc, worker, doc.assigned_to_name, dispatched=True)

	described = _describe_task(dict(doc.as_dict()))
	warnings = []
	if creates_record in scouting.DEFERRED_RECORDS:
		# v0.115.0. NOT A WARNING — a note about WHEN, not WHETHER. This record
		# is produced by an idempotent sweep rather than at completion, which is
		# what this app has instead of document hooks. A caller told the same
		# thing the unbuildable case is told would go looking for a record that
		# is coming.
		warnings.append(
			f"{creates_record} is written by `{scouting.DEFERRED_RECORDS[creates_record]}` rather "
			"than by the completion itself — an idempotent sweep an operator can see, switch off "
			"and re-run, which is what erpnext_mcp has instead of document hooks. The completion "
			"keeps the submitted measurements on this task and the sweep reads them from there "
			"together with the assignment's location fix, photographs and findings. Run the sweep "
			"over the completion date to file the record immediately."
		)
	elif creates_record and creates_record not in inspections.BUILDERS:
		warnings.append(
			f"{creates_record} exists on this site but erpnext_mcp has no builder for it, so "
			"completing this task will file the evidence against the assignment and report that it "
			f"could not produce the record itself. The three it can build are: "
			f"{', '.join(sorted(inspections.BUILDERS))}."
		)
	if not location:
		warnings.append(
			"This task names no location, so it cannot be routed to whoever is already stood in the "
			"right place. Legitimate for desk work — a certificate renewal happens at a desk."
		)
	if described["dispatch_mode"] == DISPATCH_DISPATCHED and not worker:
		warnings.append(
			"Dispatch mode is Dispatched and nobody is assigned, so this task will sit in Available "
			"and no worker can claim it. Somebody has to be sent with assign_farm_task."
		)
	shift_note = _shift_note(farm_shift)
	if shift_note:
		warnings.append(shift_note)
	# Raised against a block that is closed to entry. Same sentence, same source
	# and same posture as the dispatch above: told, not refused.
	warnings.extend(_rei_warnings(dict(doc.as_dict())))

	data = {**described, "assignment": assignment}
	if phi_override:
		data["phi_override"] = phi_override
		warnings.append(
			"This Harvest task was raised INSIDE a live pre-harvest interval on an override. "
			f"Reason given: {phi_override['reason']}"
		)
	if warnings:
		data["warnings"] = warnings
	return ToolResult(
		data=data,
		summary=(
			f"raised farm task {doc.name} ({doc.task_type}, {doc.urgency}, {doc.state})"
			+ (f" assigned to {doc.assigned_to_name}" if worker else "")
		),
		docstatus_delta="none → 0 (created)",
	)


def _evidence_argument(args: dict) -> dict:
	"""The evidence contract off the arguments, refused early and by name.

	The controller refuses it too — this is the second door, and it exists so the
	refusal a caller sees names the ARGUMENT rather than the field, and arrives
	before anything has been inserted.
	"""
	from ..erpnext_mcp.doctype.farm_task.farm_task import parse_evidence_required

	raw = args.get("evidence_required")
	if raw in (None, ""):
		raise ToolError(
			"evidence_required is required and this is the point of the whole doctype. Say what "
			"closing this task obliges somebody to produce, as JSON — for example "
			'{"photos": true, "signature": true, "findings_text": true}. The keys are: '
			f"{', '.join(sorted(EVIDENCE_KEYS))}. A task that requires no evidence is a task that "
			"gets closed with a tick in a box, and a tick in a box is what an auditor is trained "
			"to disbelieve. Nothing was created."
		)
	return parse_evidence_required(raw)


def _materials_argument(args: dict, key: str = "materials_used") -> list:
	"""The materials list off the arguments, refused early and by name.

	v0.69.0. REFUSED RATHER THAN WARNED ABOUT, and it is the one place in the
	stock path where that is true — `stock_bridge` never raises once a completion
	is under way, because a shed count must not cost somebody their compliance
	record. This is BEFORE any of that: nothing has been written, the person who
	typed the list is still there, and a silent drop would mean a chemical went on
	a block and came off no count with nobody told.
	"""
	from .. import stock_bridge

	try:
		return stock_bridge.parse_materials(args.get(key), key)
	except stock_bridge.MaterialsError as exc:
		raise ToolError(f"{exc} Nothing was written.") from None


def _draw_down_materials(task: dict, assignment_doc, args: dict) -> tuple[dict, dict]:
	"""(materials_consumed, spray_windows) for one completion. NEVER RAISES.

	THE TWO SOURCES, AND WHICH ONE WINS. An explicit `materials_used` on the
	completion is what the worker says they actually used, and it outranks
	everything — the plan is not the event. Where the completion says nothing and
	the task is a SPRAY, the task's own tank mix is used instead: that is what the
	applicator was sent to put on the block, the label rate is what went in the
	tank, and a spray whose chemicals came off no count is the failure this hook
	exists to end. For any other task type, silence means nothing was consumed —
	guessing that a repair used the parts somebody once listed would issue stock
	on a task whose whole point may have been that the part was not needed.

	NOTHING IN HERE CAN FAIL THE COMPLETION. Every write is inside
	`stock_bridge`, which catches per line; the two calls around it are wrapped
	as well, so a site mid-migrate or an Item register this app cannot read costs
	a warning rather than a filed piece of work.
	"""
	from .. import stock_bridge

	consumed = {
		"source": None,
		"materials": [],
		"stock_entries": [],
		"warnings": [],
		"requested": 0,
		"moved": 0,
	}
	windows: dict = {}
	try:
		explicit, warnings = stock_bridge.materials_from_record(args.get("materials_used"), "materials_used")
		if explicit:
			materials, source = explicit, "completion_argument"
		else:
			planned, planned_warnings = stock_bridge.materials_from_record(
				task.get("materials_used"), "the task's materials_used"
			)
			spray = str(task.get("task_type") or "") == SPRAY_TASK_TYPE
			materials = planned if spray else []
			source = "task_tank_mix" if (spray and planned) else None
			warnings = warnings + (planned_warnings if spray else [])
		consumed["source"] = source
		consumed["materials"] = materials
		consumed["warnings"] = list(warnings)
		if not materials:
			return consumed, windows

		moved = stock_bridge.issue_materials(
			materials,
			company=str(task.get("company") or ""),
			source_doctype=FARM_TASK,
			source_name=str(assignment_doc.task),
			posting_datetime=str(assignment_doc.completed_at or ""),
		)
		consumed["stock_entries"] = moved["stock_entries"]
		consumed["warnings"] = consumed["warnings"] + moved["warnings"]
		consumed["requested"] = moved["requested"]
		consumed["moved"] = moved["moved"]

		if str(task.get("task_type") or "") == SPRAY_TASK_TYPE:
			windows = stock_bridge.spray_windows(materials, str(assignment_doc.completed_at or ""))
	except Exception as exc:  # a completion is never lost to a stock failure
		consumed["warnings"].append(
			f"the materials drawdown did not run: {type(exc).__name__}: {exc}. The completion, its "
			"evidence and its compliance record are unaffected — issue the stock by hand."
		)
	return consumed, windows


def _open_assignment(task_doc, worker: str, worker_name: str, dispatched: bool, farm_shift: str = "") -> dict:
	assignment = frappe.new_doc(FARM_TASK_ASSIGNMENT)
	assignment.task = task_doc.name
	assignment.task_name = task_doc.task_name
	assignment.company = task_doc.company
	assignment.assigned_to = worker
	assignment.assigned_to_name = worker_name
	assignment.state = CLAIMED
	assignment.dispatched_by_foreman = 1 if dispatched else 0
	assignment.claimed_at = frappe.utils.now()
	# v0.64.0. THE ASSIGNMENT'S SHIFT DEFAULTS FROM THE TASK'S AND IS NOT THE SAME
	# FIELD. The task's says which shift the work was RAISED for; this one says
	# which it was DONE on, and they diverge the moment a job dispatched for the
	# morning gets finished after lunch. The default is right far more often than
	# it is wrong, and every later call that knows better overwrites it.
	assignment.farm_shift = (
		farm_shift or (task_doc.get("farm_shift") if hasattr(task_doc, "get") else None) or None
	)
	assignment.insert(ignore_permissions=True)
	return _describe_assignment(dict(assignment.as_dict()))


def _push_assignment(task: dict, worker: str, reassigned: bool = False) -> dict:
	"""Ring the phone of the person who has just been sent to this task. v0.107.0.

	WHY THE DISPATCH PUSHES AND THE CLAIM DOES NOT. `claim_farm_task` is somebody
	taking work off the board with the app already open in their hand; telling
	their own handset about it is a notification for something they just did.
	This is the other direction — a foreman sends somebody, and until now the
	worker found out the next time they happened to open the app, which on a
	picking crew is at lunch.

	CALLED LAST, AFTER EVERY WRITE — BUT STILL INSIDE THE TRANSACTION, and it is
	worth being exact about that rather than comfortable. `lock_task` takes a
	`SELECT ... FOR UPDATE` at the top of `assign_farm_task` and Frappe holds it
	until the REQUEST commits, which is after this tool returns. So this does fire
	inside the locked window, and a reader arriving in the gap would not yet see
	the row the notification describes.

	That gap is left open on purpose. Closing it properly needs an after-commit
	hook, which this app uses nowhere — `_push_break` pushes inline within its own
	transaction too, and one path doing it differently would be a second mechanism
	to reason about for a race whose window is the milliseconds between here and
	the commit. The path that would lose is push → APNs → Apple → handset → a
	person noticing → a tap → a fetch, and it is seconds at its very fastest.
	Ordering it last means the only writes that could still fail after the send
	are none, so the realistic worst case stays "a dispatch nobody was told about"
	rather than "a phone pointed at a task that is not there".

	If this ever moves to a queue or the transaction grows longer, that reasoning
	stops holding and the send belongs on a commit hook.

	NEVER RAISES, AND THE REPORT GOES ON THE ANSWER. Same contract the break horn
	has: the assignment is the record, the push is a convenience on top of it, and
	a site with no p8 key, no network or no enrolled handset must dispatch exactly
	as it did before. The report is returned rather than swallowed so a foreman
	who asks "did their phone buzz" gets `no_tokens` instead of silence.
	"""
	try:
		return push_service.send_push_to_employees(
			[worker],
			push_service.task_payload(
				task=str(task.get("name") or ""),
				task_name=str(task.get("task_name") or ""),
				location=str(task.get("location") or ""),
				urgency=str(task.get("urgency") or ""),
				reassigned=reassigned,
			),
			# The docname, so a task dispatched, taken off somebody and given
			# back replaces its own notification on the lock screen rather than
			# stacking three of them about one job.
			collapse_id=str(task.get("name") or ""),
		)
	except Exception as error:  # pragma: no cover - send_push_to_employees is itself wrapped
		return {
			"employees": 0,
			"tokens": 0,
			"sent": 0,
			"failed": 0,
			"skipped": 0,
			"reason": f"error: {error}",
		}


# ── 2. assign_farm_task ─────────────────────────────────────────────────────
def assign_farm_task(args: dict) -> ToolResult:
	"""Send one named person to one task. The foreman's half of the dual mode."""
	_require()
	row = task_row(as_str(args, "task", required=True))
	# THE SAME LOCK `claim_farm_task` TAKES, and for a race that is worse here.
	# `reassign` exists to stop work being taken off somebody who may already be
	# standing in front of it — and that guard is `live_assignment` returning
	# nothing, read before the write. A worker claiming the task in the gap makes
	# it return nothing when there IS a holder, so the foreman's assignment goes
	# through without the flag ever being asked for and the claimant is silently
	# displaced. Locking the row makes the two calls serialise: whichever lands
	# second sees what the first did. See `lock_task`.
	lock_task(row["name"])
	row = task_row(row["name"])
	worker = _worker(args, "assigned_to")
	worker_name = _worker_name(worker, as_str(args, "assigned_to_name"))

	if row["state"] in TERMINAL_STATES:
		raise ToolError(
			f"{row['name']} is {row['state']} — the work is finished or abandoned, and reassigning it "
			"would rewrite history rather than dispatch anybody. Raise a fresh task. Nothing was "
			"changed."
		)

	# Sending somebody to pick a block is the second moment harvest is initiated
	# on it, and the one where a name goes onto the record. Same guard, same
	# override, and it runs before the reassignment questions because a task
	# nobody may work is not a task worth arguing about who holds.
	phi_override = _refuse_harvest_inside_phi(row, args, "changed", override_tool="assign_farm_task")

	# v0.98.0. AND FOR THE SAME REASON, ONE LINE LATER: whether this PERSON may
	# do this KIND of work at all. There is no override argument beside it —
	# unlike the pre-harvest interval, which a licensed applicator may lawfully
	# shorten and which therefore has one — because nobody can consent a
	# sixteen-year-old into being a lawful pesticide handler.
	minor = _refuse_a_minor_on_prohibited_work(worker, worker_name, row, verb="changed")

	held = live_assignment(row["name"])
	reassigned_from = None
	if held:
		current = dict(
			frappe.db.get_value(
				FARM_TASK_ASSIGNMENT, held, ["assigned_to", "assigned_to_name", "state"], as_dict=True
			)
			or {}
		)
		if current.get("assigned_to") == worker:
			raise ToolError(f"{row['name']} is already held by {worker_name}. Nothing was changed.")
		if not as_bool(args, "reassign", False):
			raise ToolError(
				f"{row['name']} is held by {current.get('assigned_to_name') or current.get('assigned_to')} "
				f"({held}, {current.get('state')}). Taking work off somebody who may already be stood "
				"in front of it is a decision, not a correction — pass reassign=true if you mean it. "
				"Nothing was changed."
			)
		reason = as_str(args, "reason")
		if not reason:
			raise ToolError(
				"reassigning work off somebody needs a reason, which is written onto their "
				"assignment. 'Taken off them with no explanation' is the record nobody can defend. "
				"Nothing was changed."
			)
		reassigned = frappe.get_doc(FARM_TASK_ASSIGNMENT, held)
		reassigned.state = REJECTED
		reassigned.rejection_reason = f"Reassigned to {worker_name} by dispatch: {reason}"
		reassigned.save(ignore_permissions=True)
		reassigned_from = current.get("assigned_to_name") or current.get("assigned_to")

	farm_shift = _shift_argument(args, str(row.get("company") or ""))

	task_doc = frappe.get_doc(FARM_TASK, row["name"])
	task_doc.assigned_to = worker
	task_doc.assigned_to_name = worker_name
	task_doc.state = CLAIMED
	# WRITTEN ONLY WHEN GIVEN. A dispatch that names no shift leaves whatever the
	# task already carried alone; blanking it would turn a link somebody set at
	# creation into a missing one every time the work changed hands.
	if farm_shift:
		task_doc.farm_shift = farm_shift
	task_doc.save(ignore_permissions=True)
	assignment = _open_assignment(task_doc, worker, worker_name, dispatched=True, farm_shift=farm_shift)

	data = {
		**_describe_task(dict(task_doc.as_dict())),
		"assignment": assignment,
		"concurrent_claims": len(concurrent_claims(worker)),
		"note": (
			"Dispatched, not claimed: the assignment records that a foreman sent this person rather "
			"than that they took it from the pool. The distinction is worth keeping — work that has "
			"to be dispatched because it needs a named licence holder is a different kind of work "
			"from work anybody with the skill can pick up."
		),
	}
	if reassigned_from:
		data["reassigned_from"] = reassigned_from
	if minor.get("is_minor"):
		data["minor"] = minor
		data["minor_note"] = (
			f"{worker_name} is {minor['age']}. This kind of task is open to them, but the "
			f"{minor['minor_band']} hour and time-of-day limits still apply to the shift they "
			f"work it on — {(minor.get('minor_limits') or {}).get('citation')}. "
			"add_worker_to_shift is where those are checked."
		)
	if minor.get("date_of_birth_missing"):
		data["date_of_birth_missing"] = minor["date_of_birth_missing"]

	# THE DISPATCH IS NOT REFUSED OVER A LIVE RESTRICTED-ENTRY WINDOW, AND THAT
	# IS DELIBERATE. Work inside an REI is lawful with the label's PPE on — 40
	# CFR §170.607 permits early entry for specific tasks — so a server refusing
	# it outright would be inventing a rule stricter than the regulation and
	# training foremen to route around this app. What the foreman is owed is the
	# sentence, at the moment they send somebody, and it is the same sentence the
	# worker will read when they scan the block: see `spray_rei.warning_line`.
	rei_warnings = _rei_warnings(dict(task_doc.as_dict()))
	if rei_warnings:
		data["warnings"] = list(data.get("warnings") or []) + rei_warnings
	if phi_override:
		data["phi_override"] = phi_override
		data["warnings"] = [
			*(data.get("warnings") or []),
			f"{worker_name} was dispatched to pick a block INSIDE a live pre-harvest interval, on "
			f"an override. Reason given: {phi_override['reason']}",
		]
		_record_override_on_task(row["name"], phi_override)

	# LAST, after every write this call makes. See `_push_assignment`.
	data["push"] = _push_assignment(dict(task_doc.as_dict()), worker, reassigned=bool(reassigned_from))

	return ToolResult(
		data=data,
		summary=f"dispatched {row['name']} to {worker_name}"
		+ (f" (taken off {reassigned_from})" if reassigned_from else "")
		+ (f" — {len(rei_warnings)} REI warning(s)" if rei_warnings else ""),
		docstatus_delta=f"{row['state']} → {CLAIMED}",
	)


def _rei_warnings(task: dict) -> list[str]:
	"""Live restricted-entry windows on the place this task sends somebody.

	NEVER RAISES AND NEVER BLOCKS. A dispatch that failed because the REI
	register would not answer would be an outage in the one part of the day a
	farm cannot pause, so a section that cannot be read is a dispatch with no
	warning on it rather than no dispatch.

	The location is taken as the block name whatever register the task names it
	in — `Spray REI.block` is keyed on the docname and `active_for_blocks` filters
	on exactly that, so a task pointing at Field 'Home-7' and a restriction on
	Field 'Home-7' meet without either having to know about the other's doctype.
	"""
	location = str(task.get("location") or "")
	asset = str(task.get("asset") or "")
	candidates = [name for name in (location, asset) if name]
	if not candidates:
		return []
	try:
		from . import spray_rei

		windows = spray_rei.active_for_blocks(candidates, str(task.get("company") or ""))
	except Exception:  # pragma: no cover - a warning, never a refusal
		return []
	return [window["warning"] for window in windows]


# ── the pre-harvest guard, and why it REFUSES where the REI above WARNS ──────
#
# THESE TWO LOOK LIKE THE SAME CHECK AND ARE OPPOSITE DECISIONS, so the
# difference is worth stating before either is read.
#
# A RESTRICTED-ENTRY INTERVAL IS A CONDITION ON ENTRY, AND ENTRY INSIDE IT IS
# LAWFUL. 40 CFR §170.607 permits early entry for specific tasks with the
# label's PPE on. A server refusing that would be inventing a rule stricter than
# the regulation and training foremen to route around this app — so `_rei_warnings`
# tells the foreman and tells the worker, and dispatches.
#
# A PRE-HARVEST INTERVAL IS A CONDITION ON THE FRUIT, AND THERE IS NO PPE FOR IT.
# Picking inside the interval produces a load with residue above tolerance (40
# CFR 180), and that is discovered at the packing house, on somebody's shipment,
# days later, traced back to a block and a date. Nothing a crew can wear changes
# it and no amount of care makes it a near miss. There is no lawful early
# harvest, so a warning would be a server that watched somebody do the one thing
# the whole record exists to prevent and printed a sentence about it.
#
# SO: refused, and refused at the moment harvest is INITIATED on a block —
# raising the task and sending somebody to it. `override_phi` exists and is
# audited rather than absent, because the date on the record can genuinely be
# wrong in the safe direction: a window stamped from a tank that only covered
# part of a block, or a label corrected after the fact. That is a decision a
# named foreman makes with a reason attached, which is exactly what an override
# with a mandatory reason is.
#
# THE WORKER'S DOOR HAS NO OVERRIDE. `claim_farm_task` refuses and names the
# foreman's tool, because "the picker on the block decided the interval did not
# apply" is not a defence anybody can offer afterwards.

#: The `task_type` this guard applies to. One value, and a constant so that the
#: guard and any future reader of it cannot disagree about what harvest is.
HARVEST_TASK_TYPE = "Harvest"


def _phi_windows(task: dict) -> list[dict]:
	"""Live pre-harvest intervals on the place this task sends somebody.

	NEVER RAISES. See `spray.phi_windows_for_blocks`: an unreadable register
	produces no windows rather than an outage in the one part of the year a farm
	cannot pause. The dates are stamped on the spray records either way and the
	scheduled compliance sweep raises `phi_harvest_window` off the same columns.
	"""
	# CASEFOLDED, because this guard runs on the ARGUMENTS in `create_farm_task` —
	# before `as_choice` has normalised them against the doctype's own options.
	# A caller that sent "harvest" would otherwise get a task of type Harvest and
	# no guard at all, which is the worst of the three possible outcomes.
	if str(task.get("task_type") or "").strip().casefold() != HARVEST_TASK_TYPE.casefold():
		return []
	candidates = [name for name in (str(task.get("location") or ""), str(task.get("asset") or "")) if name]
	if not candidates:
		return []
	try:
		from . import spray

		return spray.phi_windows_for_blocks(candidates, str(task.get("company") or ""))
	except Exception:  # pragma: no cover - a guard that cannot read refuses nothing
		return []


def _refuse_harvest_inside_phi(task: dict, args: dict, verb: str, override_tool: str = "") -> dict | None:
	"""Refuse a harvest inside a live pre-harvest interval. Returns the override, or None.

	`override_tool` empty means THIS CALLER HAS NO OVERRIDE — the worker's door.
	It changes the sentence and nothing else: the refusal names the tool that
	does, so somebody standing on a block is told who can act rather than only
	that they cannot.
	"""
	windows = _phi_windows(task)
	if not windows:
		return None

	# NOT `override_tool or ...`. An empty `override_tool` is the WORKER'S door,
	# where there is no override to ask for; a non-empty one is a foreman's, where
	# the argument decides. Getting this the wrong way round refuses every foreman
	# and lets every worker through, which is exactly backwards and is what the
	# `TheWorkersDoorHasNoOverride` tests exist to catch.
	if not override_tool or not as_bool(args, "override_phi", False):
		latest = windows[0]
		opens_on = _phi_opens_on(latest)
		raise ToolError(
			f"{latest['block']} is inside a pre-harvest interval until {opens_on} "
			+ (f"({latest['phi_source_item']}, " if latest.get("phi_source_item") else "(")
			+ f"{latest['source']})"
			+ (
				f", and {len(windows) - 1} more spray(s) on it are still inside theirs"
				if len(windows) > 1
				else ""
			)
			+ ". A pick inside the interval is a residue violation on a shipped load — it is found "
			"at the packing house, days later, and traced back to this block and this date. Unlike "
			"a restricted-entry interval there is no PPE that makes it lawful, so this is refused "
			"rather than warned about. "
			+ (
				f"A foreman who knows the stamped date is wrong — a tank that covered part of the "
				f"block, a label corrected since — passes override_phi=true with "
				f"phi_override_reason to {override_tool}, which records who decided and why."
				if override_tool
				else "There is no override on this tool: 'the picker decided the interval did not "
				"apply' is not a defence anybody can offer at the packing house. A foreman "
				"dispatches it with assign_farm_task if the stamped date is genuinely wrong."
			)
			+ f" Nothing was {verb}."
		)

	reason = as_str(args, "phi_override_reason").strip()
	if not reason:
		raise ToolError(
			"override_phi=true needs phi_override_reason. An override with no reason is "
			"indistinguishable afterwards from a guard that was never there, and the reason is "
			f"the only part of this that survives to the packing house. Nothing was {verb}."
		)
	return {
		"overridden": True,
		"reason": reason,
		"windows_overridden": [
			{
				"block": window["block"],
				"opens_on": _phi_opens_on(window),
				"phi_source_item": window.get("phi_source_item"),
				"source_doctype": window["source_doctype"],
				"source": window["source"],
			}
			for window in windows
		],
		"note": (
			"This pick was authorised inside a live pre-harvest interval. The reason is on the "
			"task and in the action log; the spray records that opened the interval are unchanged, "
			"so the compliance alert stands until the date passes."
		),
	}


def _record_override_on_task(task: str, override: dict) -> None:
	"""Write the override onto an EXISTING task's notes. Never raises.

	The dispatch path's half of `_with_override_note`, which the creation path
	uses on a document that has not been inserted yet. A note that could not be
	appended must not undo a dispatch that succeeded — the override is in the
	action log either way — so this reports nothing and swallows nothing else.
	"""
	try:
		current = str(frappe.db.get_value(FARM_TASK, task, "notes") or "")
		frappe.db.set_value(FARM_TASK, task, "notes", _with_override_note(current, override))
	except Exception:  # pragma: no cover - a note is never worth losing a dispatch
		pass


def _with_override_note(notes: str, override: dict | None) -> str:
	"""Append the override to the task's own notes, or leave them alone.

	THE REASON HAS TO SURVIVE ON THE TASK and not only in the action log. A load
	questioned at the packing house is traced to a block and a date, and the
	record somebody pulls up is the task — an override that lived only in a log
	nobody opens is an override that was never explained.
	"""
	if not override:
		return notes
	stamp = (
		"PRE-HARVEST INTERVAL OVERRIDDEN. This pick was authorised inside a live PHI window on "
		+ ", ".join(
			f"{entry['block']} (opens {entry['opens_on']}, {entry['source']})"
			for entry in override["windows_overridden"]
		)
		+ f". Reason: {override['reason']}"
	)
	return f"{notes}\n\n{stamp}" if notes else stamp


def _phi_opens_on(window: dict) -> str:
	"""The first date the block may be picked, as the guard's messages say it."""
	from . import spray

	return spray._day_after(window.get("phi_clears_on") or "")


# ── 3. claim_farm_task ──────────────────────────────────────────────────────
def claim_farm_task(args: dict) -> ToolResult:
	"""A worker takes one task from the pool. Capped at three at once, per worker."""
	_require()
	row = task_row(as_str(args, "task", required=True))
	# THE LOCK GOES BEFORE THE STATE IS TRUSTED, AND THE ROW IS THEN READ AGAIN.
	# The first read above is what turns a bad docname into a sentence about
	# `list_dispatch_board`; it is NOT the read this function may decide on,
	# because between it and the save another claimer can have taken the task.
	# Everything from here to `task_doc.save` runs inside the row lock. See
	# `lock_task`.
	lock_task(row["name"])
	row = task_row(row["name"])
	worker = _worker(args)
	worker_name = _worker_name(worker, as_str(args, "worker_name"))

	if row["state"] != AVAILABLE:
		if row["state"] == DRAFT:
			raise ToolError(
				f"{row['name']} is still a Draft and is not in the pool. Somebody has to publish it "
				"first. Nothing was changed."
			)
		if row["state"] in TERMINAL_STATES:
			raise ToolError(
				f"{row['name']} is {row['state']}. There is nothing to claim. Nothing was changed."
			)
		held = frappe.db.get_value(FARM_TASK, row["name"], "assigned_to_name") or "somebody else"
		raise ToolError(
			f"{row['name']} is {row['state']} and held by {held}. Two people stood in front of the "
			"same work both believing it is theirs is exactly what a dispatch board exists to "
			"prevent. Nothing was changed."
		)

	# THE WORKER'S DOOR, AND IT HAS NO OVERRIDE. `override_tool` names the
	# foreman's tool instead, so somebody standing on a block is told who can act
	# rather than only that they cannot.
	_refuse_harvest_inside_phi(row, args, "changed")

	if (row.get("dispatch_mode") or "Either") not in SELF_PICKABLE:
		raise ToolError(
			f"{row['name']} is dispatch_mode Dispatched: somebody has to be SENT to it by name. That "
			"is how this app marks work where the named holder matters — a licence, a safety-critical "
			"repair — and self-picking it would put the wrong person's name on a regulated record. A "
			"foreman assigns it with assign_farm_task. Nothing was changed."
		)

	holding = concurrent_claims(worker)
	if len(holding) >= MAX_CONCURRENT_CLAIMS:
		raise ToolError(
			f"{worker_name} is already holding {len(holding)} task(s): {', '.join(sorted(holding))}. "
			f"The limit is {MAX_CONCURRENT_CLAIMS} at once. This is a hoarding limit and not a "
			"productivity one — completing or rejecting one frees a slot in the same instant, and "
			"the point is that nobody can pull the whole pool onto their own name and leave a board "
			"that looks worked. Nothing was changed."
		)

	task_doc = frappe.get_doc(FARM_TASK, row["name"])
	task_doc.assigned_to = worker
	task_doc.assigned_to_name = worker_name
	task_doc.state = CLAIMED
	task_doc.save(ignore_permissions=True)
	assignment = _open_assignment(task_doc, worker, worker_name, dispatched=False)

	claimed = {
		**_describe_task(dict(task_doc.as_dict())),
		"assignment": assignment,
		"concurrent_claims": len(holding) + 1,
		"claims_remaining": MAX_CONCURRENT_CLAIMS - len(holding) - 1,
		"evidence_you_will_need": _contract_sentence(evidence_contract(task_doc.evidence_required)),
	}
	# v0.79.0. THE HINT, NOT THE MERGE. A claim is the moment somebody has
	# decided to do this job, which is exactly when telling them that another
	# open task names the same valve is useful and not yet too late. Nothing is
	# merged: two reports of a valve are sometimes two valves.
	#
	# NO AUTO-PAUSE HERE, and the asymmetry with `start_farm_task` is deliberate.
	# Claiming is planning a morning — a worker may hold three — and pausing
	# somebody's running job because they picked up their next one would stop the
	# clock on work they are still doing.
	hint = _duplicate_hint(dict(task_doc.as_dict()))
	if hint:
		claimed["duplicate_hint"] = hint

	return ToolResult(
		data=claimed,
		summary=f"{worker_name} claimed {row['name']} ({len(holding) + 1} of {MAX_CONCURRENT_CLAIMS} held)",
		docstatus_delta=f"{AVAILABLE} → {CLAIMED}",
	)


# ── 4. start_farm_task ──────────────────────────────────────────────────────
def start_farm_task(args: dict) -> ToolResult:
	"""Clock in on one claimed task. The start of the hour that gets charged to it."""
	_require()
	assignment = _assignment_for(args)
	worker = _worker(args, required=False)
	if worker and assignment.get("assigned_to") != worker:
		raise ToolError(
			f"{assignment['task']} is held by {assignment.get('assigned_to_name')}, not {worker}. "
			"Nothing was changed."
		)
	if assignment.get("state") == IN_PROGRESS:
		raise ToolError(
			f"{assignment['name']} was already started at {assignment.get('started_at')}. Starting it "
			"twice would move the clock-in forward and shorten the hour actually spent. Nothing was "
			"changed."
		)
	if assignment.get("state") != CLAIMED:
		raise ToolError(
			f"{assignment['name']} is {assignment.get('state')}, not {CLAIMED}. Nothing was changed."
		)

	task = task_row(assignment["task"])
	farm_shift = _shift_argument(args, str(task.get("company") or ""))

	# ONE TASK IN PROGRESS PER WORKER, ENFORCED BY PAUSING RATHER THAN REFUSING.
	# Somebody standing at a broken valve does not want to be told to go and tidy
	# up the job they walked away from; they want the valve fixed. So whatever
	# they had running is stood down, the answer says so, and nobody has to route
	# around this app to do the urgent thing. See the v0.79.0 block below.
	auto_paused = _auto_pause_for(
		str(assignment.get("assigned_to") or ""),
		exclude_task=str(assignment["task"]),
		reason=f"Started {assignment['task']}",
	)

	doc = frappe.get_doc(FARM_TASK_ASSIGNMENT, assignment["name"])
	doc.state = IN_PROGRESS
	doc.started_at = as_str(args, "started_at") or frappe.utils.now()
	_start_segment(doc, str(doc.started_at))
	if farm_shift:
		doc.farm_shift = farm_shift
	elif not doc.farm_shift:
		# v0.64.0. THE CLOCK-IN IS THE LAST CHEAP MOMENT TO LEARN WHICH SHIFT THIS
		# IS. Nobody types a shift docname into a phone, and a completion that
		# reaches the shift's compliance record only when somebody remembered to
		# pass an argument is a compliance record with holes in it on exactly the
		# busy days it matters. Inferred ONLY from an open shift this worker is
		# actually rostered on at this moment, and only when there is exactly one
		# — two open shifts naming the same person is an ambiguity, and guessing
		# would put the evidence on the wrong crew's record, which is worse than
		# leaving it unlinked and saying so.
		doc.farm_shift = _open_shift_for(doc.assigned_to, str(task.get("company") or "")) or None
	doc.save(ignore_permissions=True)
	_set_task_state(assignment["task"], IN_PROGRESS)

	started = {
		"assignment": _describe_assignment(dict(doc.as_dict())),
		"task": _describe_task(task_row(assignment["task"])),
		"evidence_you_will_need": _contract_sentence(evidence_contract(task.get("evidence_required"))),
		"note": (
			"This is the clock-in for THIS TASK, not for the shift. A worker on the clock all "
			"morning did this particular cabin between ten and half past, and that is what an "
			"hour charged to a job has to mean."
		),
		"shift_note": (
			f"This work is anchored to shift {doc.farm_shift}, so its completion evidence will "
			"land on that shift's compliance timeline beside the weather it was done in."
			if doc.farm_shift
			else "This work is anchored to NO SHIFT, so its completion evidence will sit on the "
			"assignment alone and nothing will reach a compliance record spanning an exposure "
			"period. Pass farm_shift here or at completion if the worker was on one."
		),
	}
	if auto_paused:
		started["auto_paused"] = auto_paused
		started["auto_pause_note"] = (
			f"{auto_paused['task_name'] or auto_paused['task']} was in progress and has been paused "
			f"so this one can run — {auto_paused['segment_minutes']} minute(s) were banked against "
			"it and nothing was lost. Resume it with resume_farm_task."
		)
	started["duplicate_hint"] = _duplicate_hint(task_row(assignment["task"]))

	return ToolResult(
		data=started,
		summary=(
			f"{doc.assigned_to_name} started {assignment['task']} at {doc.started_at}"
			+ (f" (paused {auto_paused['task']})" if auto_paused else "")
		),
		docstatus_delta=f"{CLAIMED} → {IN_PROGRESS}",
	)


# ── 5. complete_farm_task ───────────────────────────────────────────────────
def complete_farm_task(args: dict) -> ToolResult:
	"""Finish one task: check the evidence, file it, and write the compliance record."""
	_require()
	assignment = _assignment_for(args, or_completed=True)
	task = task_row(assignment["task"])
	worker = _worker(args)

	# A PARENT DOES NOT CLOSE WHILE A STEP IS LIVE. This is what makes a
	# multi-day investigation survive an evening: without it the first person to
	# finish their piece closes the whole thing, and the camera footage nobody
	# pulled becomes a finding nobody made. The refusal names the steps, because
	# "it is not finished" is useless and "you are waiting on the witness
	# interview" is actionable.
	blocking = _blocking_subtasks(task["name"])
	if blocking and assignment.get("state") != COMPLETED:
		waiting = ", ".join(f"{child['name']} ({child['task_name']})" for child in blocking)
		raise ToolError(
			f"{task['name']} has {len(blocking)} step(s) still open: {waiting}. A parent task is "
			"finished when its steps are — closing it now would file an investigation whose "
			"outstanding work is invisible from the record that is supposed to carry it. Finish or "
			"reject the steps first, or reject this one with a reason. Nothing was changed."
		)

	if assignment.get("assigned_to") != worker:
		raise ToolError(
			f"{assignment['task']} is held by "
			f"{assignment.get('assigned_to_name') or assignment.get('assigned_to')}, not {worker}. A "
			"completion filed by somebody who was not there is not a chain of custody, it is a "
			"rumour — and it is the first thing an auditor pulls on. Nothing was changed."
		)
	# v0.20.1. A COMPLETION THAT ARRIVES TWICE IS ONE COMPLETION. See
	# `_replayed` — the refusal below still stands for every finished assignment
	# whose signature does not match, which is the case where two different
	# submissions are genuinely in conflict.
	if assignment.get("state") == COMPLETED:
		replay = _replayed(assignment, task, worker, args)
		if replay is not None:
			return replay
	if assignment.get("state") not in (CLAIMED, IN_PROGRESS):
		raise ToolError(
			f"{assignment['name']} is {assignment.get('state')} and cannot be completed. Nothing was changed."
		)

	contract = evidence_contract(task.get("evidence_required"))
	# v0.69.0. THE MATERIALS ARGUMENT IS CHECKED HERE, WITH THE EVIDENCE, AND
	# NOT LATER. It is the only stock refusal in the whole completion path (see
	# `_draw_down_materials` for why every other one is a warning) and it belongs
	# where every other refusal is: before anything has been written, while the
	# client that sent the list can still correct it. The value is thrown away —
	# `_draw_down_materials` re-reads it — because this is a validation and not a
	# computation, and doing it twice costs nothing next to a partial write.
	_materials_argument(args)
	evidence = inspections.normalise_evidence(args.get("evidence_files"), "evidence_files")
	signature = as_str(args, "signature_file")
	narrative = as_str(args, "completion_narrative")
	witness = as_str(args, "witness")
	findings_given = "findings_text" in args
	findings = as_str(args, "findings_text")

	clean_pass = clean_pass_flag(args)
	if clean_pass is True:
		# An explicit "I walked it and found nothing" satisfies a contract that
		# demands findings_text. See `clean_pass_flag` for why the flag has to
		# exist at all.
		findings_given = True
	elif clean_pass is False and not findings.strip():
		raise ToolError(
			"clean_pass=false says something was found, and findings_text is empty. The whole "
			"value of the flag is that it is the worker's own answer — an answer of 'issues "
			"found' with nothing beside it opens a corrective action that names no fault, which "
			"is the one thing an auditor cannot act on. Write what was wrong. Nothing was changed."
		)

	# v0.41.0. THE CHECKLIST IS CHECKED BEFORE THE EVIDENCE CONTRACT and both are
	# reported before anything is written. The order is deliberate: an unticked
	# required item is a statement that part of the WORK was not done, and the
	# evidence contract is about what the work PRODUCED — telling somebody their
	# photograph is missing when the real answer is that they never tested the CO
	# detector sends them back for the wrong thing.
	checklist_state = _marked_checklist(task, args)
	outstanding = unmet_checklist(checklist_state)
	if outstanding:
		raise ToolError(
			f"{assignment['task']} cannot be completed: "
			f"{len(outstanding)} required checklist item(s) are not marked done.\n"
			+ "\n".join(f"  - {item}" for item in outstanding)
			+ "\n\nThe checklist came off the template this task was raised from and was "
			"snapshotted onto the task at creation, so it is the list the worker was shown. Mark "
			"them with the `checklist` argument — a list of item names, or "
			'[{"item_name": "…", "done": true, "note": "…"}] where you want to record what was '
			"found. An item that genuinely does not apply should be OPTIONAL on the template "
			"rather than falsely ticked here. Nothing was changed and no compliance record was "
			"written."
		)

	# v0.115.0. THE FIX ON THE SUBMISSION, OR THE ONE ALREADY ON THE ASSIGNMENT.
	# A worker who sent their coordinates when they claimed the job has met a
	# `gps` contract without sending them twice, and demanding a second copy at
	# completion would refuse a submission whose evidence is already on file.
	location_gps = as_str(args, "farm_location_gps") or str(assignment.get("farm_location_gps") or "")
	unmet = _unmet_evidence(contract, evidence, signature, findings_given, witness, location_gps)
	if unmet:
		raise ToolError(
			f"{assignment['task']} cannot be completed: its evidence contract is not met.\n"
			+ "\n".join(f"  - {line}" for line in unmet)
			+ "\n\nThis is the refusal the whole doctype exists for. The contract was stated when "
			"the task was raised, precisely so that closing it could not be a tick in a box. "
			"Nothing was changed and no compliance record was written — fix the submission and "
			"call again."
		)

	now = frappe.utils.now()
	doc = frappe.get_doc(FARM_TASK_ASSIGNMENT, assignment["name"])
	doc.state = COMPLETED
	doc.completed_at = as_str(args, "completed_at") or now
	doc.completion_narrative = narrative
	doc.findings_text = findings
	doc.witness = witness
	# v0.19.1. FSMA §112.161(a)(1)(i) wants the location as well as the name, and
	# the completion is where the location is knowable — the phone is standing in
	# it. Written only when given: a blank is "nobody recorded it", and overwriting
	# a location somebody already put on the assignment with an empty string would
	# turn a recorded fact into a missing one.
	location_gps = as_str(args, "farm_location_gps")
	if location_gps:
		doc.farm_location_gps = location_gps
	# v0.64.0. THE LAST CHANCE TO SAY WHICH SHIFT THIS WAS DONE ON, and the one
	# a client that only ever calls `complete_farm_task` has. Written only when
	# given, on the same argument `farm_location_gps` is and for the same reason:
	# blanking a link the clock-in already established would silently detach a
	# completion from the compliance record it belongs on.
	completion_shift = _shift_argument(args, str(task.get("company") or ""))
	if completion_shift:
		doc.farm_shift = completion_shift
	elif not doc.farm_shift:
		doc.farm_shift = _open_shift_for(worker, str(task.get("company") or "")) or None
	doc.signature_file = signature or doc.signature_file
	# v0.79.0. THE SUM OF THE SEGMENTS, NOT THE WALL CLOCK. A worker interrupted
	# twice spent three stretches on this job and the gaps belong to whatever
	# they were doing instead — see `active_minutes`, which falls back to the old
	# arithmetic for every assignment written before segments existed. A stated
	# `actual_duration_minutes` still wins: a foreman correcting a figure is
	# making a claim about the past that this app does not get to overrule.
	_close_segment(doc, str(doc.completed_at), SEGMENT_COMPLETION)
	doc.paused_at = None
	doc.actual_duration_minutes = as_int(args, "actual_duration_minutes") or active_minutes(doc)
	for row in evidence:
		doc.append("evidence_files", dict(row))

	# v0.20.1. THE VISIT AND THE SIGNATURE, both written here and nowhere else.
	#
	# `visit_id` is written ONLY WHEN GIVEN, on the same argument `farm_location_gps`
	# is: a client that does not mint one leaves whatever is already on the row
	# alone, and blanking it would turn a trip somebody can see in `list_visits`
	# into five unrelated completions.
	#
	# WHAT IS GIVEN IS NOW CHECKED. `as_visit_id` refuses anything that is not the
	# handset's UUID rather than writing it through — the format is confirmed, and
	# a garbled identifier in this column does not look like an error downstream,
	# it looks like a different visit.
	#
	# The signature is hashed from THE PAYLOAD AS IT ARRIVED — `as_str(args,
	# "completed_at")` and not `doc.completed_at`, because the second one is the
	# server's `now()` where the client sent nothing, and `now()` differs on every
	# retry. Hashing it would make the record unmatchable by the very resubmission
	# it exists to recognise. See `erpnext_mcp/completions.py`.
	visit = as_visit_id(args)
	if visit:
		doc.visit_id = visit
	doc.completion_signature = completions.signature(
		assignment["name"],
		worker,
		evidence,
		findings,
		narrative,
		as_str(args, "completed_at"),
	)

	# WHAT GOES ON THE COMPLIANCE RECORD IS NOT ALWAYS WHAT THE WORKER TYPED.
	# `records.branch_state` reads a record's state off its findings text —
	# non-empty means Corrective Action Required — so a worker who typed the
	# literal words "clean pass" into a field the contract made mandatory would
	# open a corrective action against a cabin that is fine. `clean_pass` is the
	# authoritative signal, so on a clean pass the RECORD's findings are empty
	# (which is how records.py spells "nothing was wrong") and the sentence goes
	# in the record's notes instead. The ASSIGNMENT keeps the worker's own words
	# either way — that is the evidence, and it is not this function's to edit.
	record_findings = "" if clean_pass is True else findings
	produced, record_note, record_state, deferred_payload = _produce_record(
		task, doc, evidence, signature, record_findings, args
	)
	if produced:
		doc.produced_record = produced
	doc.save(ignore_permissions=True)

	# v0.64.0. THE EVIDENCE FLOWS ONTO THE SHIFT AFTER THE ASSIGNMENT IS SAVED
	# AND NEVER BEFORE. The assignment is the record of what was produced; the
	# shift event is a pointer to it. Writing the pointer first would leave a
	# timeline entry naming a completion that a later failure meant never existed.
	shift_flow = _flow_evidence_into_shift(doc, task, evidence, clean_pass)

	# v0.69.0. THE STOCK MOVES AFTER THE ASSIGNMENT IS SAVED, for exactly the
	# reason the shift event does — and with one extra promise on top of it. The
	# completion is the compliance record; the stock entry is a consequence of it.
	# Issuing first would leave five litres off a shed count for a completion a
	# later failure meant never existed, and — the part that matters more — a
	# drawdown that could not be written must never travel back up this function
	# as an error. It cannot: see `_draw_down_materials`.
	materials_consumed, spray_window = _draw_down_materials(task, doc, args)

	# v0.64.0. AND THEN ASK THE CALENDAR TO LOOK AGAIN. Same reason, same moment:
	# the world has changed and the record says so, and a worker who files two
	# photographs and a signature and watches the alert that sent them there sit
	# unchanged until two in the morning learns that the calendar is decoration.
	compliance_eval = _evaluate_compliance_after(
		task, str(task.get("creates_record") or "").strip(), str(task.get("company") or "")
	)

	final_state = AWAITING_REVIEW if record_state == records.CORRECTIVE_ACTION_REQUIRED else COMPLETED
	task_fields = {"produced_record": produced or ""}
	# v0.115.0. WHAT THE WORKER SENT, KEPT WHERE THE SWEEP WILL LOOK FOR IT. The
	# task's own defaults are already in here — `_produce_record` merged them
	# under the submission — so this is the whole answer for the record, not a
	# fragment of it, and re-reading the template later cannot change what a
	# completed round said. Written ONLY on the deferred path: on every other
	# task `creates_record_data` is the plan somebody was sent with, and
	# overwriting it with a copy of itself would be noise.
	if deferred_payload is not None:
		task_fields["creates_record_data"] = json.dumps(deferred_payload)
	# The ticks are written back ONLY where there is a checklist, so a task
	# without one never has an empty blob stamped over the default.
	if checklist_items(checklist_state):
		task_fields["checklist_status"] = json.dumps(checklist_state)
	# v0.69.0. WHAT WAS ACTUALLY USED, AND WHAT THE DRAWDOWN DID, ON THE TASK.
	# `materials_used` is overwritten only when the completion NAMED a list —
	# where the tank mix came off the task itself, rewriting it with a copy of
	# itself would be noise, and where nothing was consumed, blanking the plan
	# would erase what somebody was sent to do.
	if materials_consumed["source"] == "completion_argument":
		task_fields["materials_used"] = json.dumps(materials_consumed["materials"])
	if materials_consumed["source"]:
		task_fields["stock_drawdown"] = json.dumps(
			{
				"source": materials_consumed["source"],
				"stock_entries": materials_consumed["stock_entries"],
				"warnings": materials_consumed["warnings"],
				"requested": materials_consumed["requested"],
				"moved": materials_consumed["moved"],
			}
		)
	# THE WINDOWS, WRITTEN ONCE AND NEVER RECOMPUTED. `stock_bridge.spray_windows`
	# has already folded the tank to its strictest product; these five columns are
	# what `rei_active_block_entry` and `phi_harvest_window` read, and stamping
	# them here — rather than having the rules join out to the labels every sweep
	# — is what stops a label correction next March silently reopening a block
	# that was posted last August.
	for column in (
		"spray_completed_at",
		"rei_expires_at",
		"rei_source_item",
		"phi_clears_on",
		"phi_source_item",
	):
		if spray_window.get(column):
			task_fields[column] = spray_window[column]
	_set_task_state(assignment["task"], final_state, **task_fields)

	# AND THEN ASK THE TWO INTERVAL RULES, in the same call and for the same
	# reason the narrowed sweep above exists: the applicator who has just shut
	# the sprayer is the person who needs to see the block posted, and "it will
	# appear within the hour" is how a crew learns to look somewhere else.
	spray_window_eval = _evaluate_spray_windows(spray_window, str(task.get("company") or ""))

	data = {
		"task": _describe_task(task_row(assignment["task"])),
		"assignment": _describe_assignment(dict(doc.as_dict())),
		"evidence_filed": len(evidence),
		"evidence_required": contract,
		"produced_record": produced,
		"produced_record_doctype": task.get("creates_record") or None,
		"produced_record_state": record_state,
		"final_state": final_state,
		"checklist": checklist_items(checklist_state),
		# v0.20.1. ALWAYS PRESENT, AND FALSE HERE. A client that has to test
		# whether the key exists before reading it has two code paths where it
		# needs one, and the one it exercises least is the one that breaks — this
		# whole release is about a retry path nobody ran until an orchard did.
		"x_idempotent": False,
		"completion_signature": doc.completion_signature,
		"visit_id": doc.visit_id or None,
		# ALWAYS PRESENT, null where this work was not anchored to a shift. A
		# client reading its absence as "it worked" would report a compliance
		# record that was never written, which is the failure this whole join
		# exists to prevent.
		"shift_evidence": shift_flow,
		# ALWAYS PRESENT, null where this completion could not have changed any
		# rule's answer — a hand-raised task from no alert that produces no
		# record is exactly that, and saying so is different from silence.
		"compliance_evaluation": compliance_eval,
		# v0.69.0. ALWAYS PRESENT, null where this completion consumed nothing —
		# same convention as `shift_evidence` above and for the same reason. A
		# client testing for the key's existence rather than its value has two
		# paths where it needs one.
		"materials_consumed": _describe_consumption(materials_consumed),
		# ALWAYS PRESENT, null where this was not a spray or where nothing in the
		# tank restricts entry or harvest. A fertiliser opens no window, and
		# saying so is different from silence.
		"spray_windows": _describe_windows(spray_window, spray_window_eval),
	}
	if record_note:
		data["record_note"] = record_note
	if materials_consumed["warnings"]:
		data["stock_note"] = (
			"THE COMPLETION SUCCEEDED AND PART OF THE DRAWDOWN DID NOT. Every warning in "
			"`materials_consumed.warnings` is a movement that was not written — the work, its "
			"evidence and its compliance record are unaffected, and no stock question can ever "
			"cost somebody a filed piece of work. Issue what is listed by hand, or fix the count "
			"and re-issue: what was used is recorded on the task either way."
		)
	if shift_flow is None:
		data["shift_note"] = (
			"This completion is anchored to NO SHIFT, so nothing reached a compliance record "
			"spanning an exposure period — the evidence is on the assignment alone. Pass "
			"farm_shift to create_farm_task, assign_farm_task, start_farm_task or this call when "
			"the work belongs to a shift. A shift is inferred at clock-in when the worker is "
			"rostered on exactly one open one."
		)
	if final_state == AWAITING_REVIEW:
		data["review_note"] = (
			"This went to Awaiting-Review because the record it produced found something. The WORK "
			"is done and the register moved forward, so the alert that asked for it will dismiss on "
			"the next sweep; what needs a person is the finding, and a Critical alert now stands "
			"against the record until it is closed or superseded. Doing the work and finding a "
			"problem are two different facts and both are true."
		)
	else:
		data["dismissal_note"] = (
			"Nothing here dismissed a Compliance Alert BY HAND, and nothing can. The record this "
			"completion wrote moved the register forward; the rules that read that register were "
			"then asked to look again, and any alert that went away did so because its own "
			"condition is no longer true. That is the only honest way for an alert to go away — "
			"changing the world and letting the sweep notice — and `compliance_evaluation` is "
			"what the sweep said. Rules outside that narrowing are untouched and reach the "
			"scheduled pass as they always did."
		)
	# v0.85.0. THE COMPLETION GOES UP THE CHAIN, FROZEN. It runs last, after the
	# record, the stock movement, the spray windows and the compliance
	# re-evaluation, so the snapshot freezes the completion as it finally stands
	# rather than half-way through. `propagate` never raises, so nothing below
	# here can cost somebody a filed piece of work — the same promise the
	# `stock_note` paragraph above makes about a failed drawdown.
	#
	# THE SUBJECT IS THE WORKER WHO DID IT, so the chain walked is theirs: the
	# question a supervisory copy answers is "what did my crew do", and a task
	# completed by somebody who reports elsewhere belongs in that person's
	# supervisor's feed and not in the dispatcher's.
	shadow = shadow_log.quiet_propagate(
		event_type=shadow_log.EVENT_TASK_COMPLETED,
		source_doctype=FARM_TASK_ASSIGNMENT,
		source_name=assignment["name"],
		subject_employee=worker,
		company=str(task.get("company") or ""),
		occurred_at=str(doc.completed_at or ""),
		summary=(
			f"{doc.assigned_to_name or worker} completed {task.get('task_name') or assignment['task']}"
			+ (f" at {task.get('location')}" if task.get("location") else "")
			+ f" with {len(evidence)} evidence file(s)"
			+ (f", producing {task.get('creates_record')} {produced}" if produced else "")
			+ f". State: {final_state}."
		),
		snapshot=shadow_log.snapshot_of(
			FARM_TASK_ASSIGNMENT,
			assignment["name"],
			{
				"_task": _describe_task(task_row(assignment["task"])),
				"_final_state": final_state,
				"_produced_record": produced,
				"_produced_record_doctype": task.get("creates_record") or None,
				"_evidence_filed": len(evidence),
			},
		),
	)
	if shadow:
		data["shadow_log"] = shadow

	return ToolResult(
		data=data,
		summary=(
			f"{doc.assigned_to_name} completed {assignment['task']} with {len(evidence)} evidence "
			f"file(s)" + (f", produced {task.get('creates_record')} {produced}" if produced else "")
		),
		docstatus_delta=f"{assignment.get('state')} → {final_state}",
	)


def _replayed_consumption(task: dict) -> dict | None:
	"""What the FIRST completion's drawdown did, read off the task. Never raises."""
	raw = str(task.get("stock_drawdown") or "").strip()
	if not raw or raw == "{}":
		return None
	try:
		stored = json.loads(raw)
	except Exception:  # pragma: no cover - a column somebody hand-edited
		return None
	if not isinstance(stored, dict) or not stored.get("source"):
		return None
	stored["materials"] = _quiet_materials(task.get("materials_used"))
	stored["replayed"] = True
	return stored


def _replayed_windows(task: dict) -> dict | None:
	"""The windows the FIRST completion stamped, read off the task. Never raises."""
	out = {
		key: task.get(key)
		for key in (
			"spray_completed_at",
			"rei_expires_at",
			"rei_source_item",
			"phi_clears_on",
			"phi_source_item",
		)
		if task.get(key)
	}
	if not (out.get("rei_expires_at") or out.get("phi_clears_on")):
		return None
	out["replayed"] = True
	return out


def _quiet_materials(raw) -> list:
	from .. import stock_bridge

	materials, _warnings = stock_bridge.materials_from_record(raw, "materials_used")
	return materials


def _describe_consumption(consumed: dict) -> dict | None:
	"""The `materials_consumed` block, or None where nothing was consumed.

	A WARNING WITH NO SOURCE IS STILL REPORTED, and that case is real: a task
	whose stored tank mix will not parse consumed nothing and has something
	important to say about why. Returning null there would turn "this task's
	materials column is corrupt and nothing was drawn down" into silence, which
	is the one shape of answer this whole path is written against.
	"""
	if not consumed.get("source") and not consumed.get("warnings"):
		return None
	out = {
		# `completion_argument` or `task_tank_mix` — WHICH LIST WAS USED, said out
		# loud. The two are different claims about the same spray: one is what the
		# worker reported using and the other is what they were sent to use, and a
		# reader reconciling a count later needs to know which they are looking at.
		"source": consumed["source"],
		"materials": consumed["materials"],
		"stock_entries": consumed["stock_entries"],
		"requested": consumed["requested"],
		"moved": consumed["moved"],
		"warnings": consumed["warnings"],
	}
	if consumed["requested"] and not consumed["moved"]:
		out["note"] = (
			"Nothing moved. What was used is recorded on the task and on this completion; the "
			"stock ledger does not know about it yet."
		)
	return out


def _describe_windows(windows: dict, evaluation: dict | None) -> dict | None:
	"""The `spray_windows` block, or None where this application opened none."""
	if not windows:
		return None
	if not (windows.get("rei_expires_at") or windows.get("phi_clears_on")):
		# The only other thing `spray_windows` returns is the note about a site
		# whose Item register cannot answer, and that is worth reporting rather
		# than flattening to null: "no restriction" and "cannot say" are the two
		# answers a person must never confuse.
		return dict(windows) or None
	out = dict(windows)
	out["evaluation"] = evaluation
	out["note"] = (
		"Both intervals are stamped on the task and neither moves again — a label corrected "
		"next season does not reopen a block that was posted this one. Each clears itself: the "
		"REI to the hour, the PHI the day after the date above."
	)
	return out


def _replayed(assignment: dict, task: dict, worker: str, args: dict):
	"""The existing completion, where this submission IS that completion. Else None.

	v0.20.1, and the reason is in `erpnext_mcp/completions.py`: an offline queue
	drained into a connection that dropped between the server's acknowledgement
	and the handset's receipt, and every re-send came back as a hard error about
	work that was already filed and evidenced.

	RETURNING None IS NOT "NO OPINION" — it is "this is a different submission",
	and the caller's refusal then stands. Two people cannot file the same
	completion, a second account of the same work is not the first one again, and
	a task genuinely completed twice by accident is a thing somebody should be
	told about rather than have quietly absorbed. The whole value of the
	signature is that it separates those from a phone asking the same question
	twice.

	NOTHING HERE WRITES. No record is produced, no state moves, no evidence row
	is appended — that is the entire promise, and it is kept by there being no
	`save` in this function. The response is rebuilt by READING the assignment
	that already exists, so three rapid retries produce three identical answers
	and one record.
	"""
	stored = str(assignment.get("completion_signature") or "").strip()
	if not stored:
		# A Completed row with no signature at all. Pre-v0.20.1 rows are given
		# one by the backfill patch; a row that still has none after that is one
		# the backfill could not read, and guessing that an unknown submission
		# matches it would turn a genuine conflict into a silent success.
		return None
	evidence = inspections.normalise_evidence(args.get("evidence_files"), "evidence_files")
	if not completions.matches(
		stored,
		assignment["name"],
		worker,
		evidence,
		as_str(args, "findings_text"),
		as_str(args, "completion_narrative"),
		as_str(args, "completed_at"),
	):
		return None

	doc = frappe.get_doc(FARM_TASK_ASSIGNMENT, assignment["name"])
	row = _assignment_row(assignment["name"])
	produced = str(row.get("produced_record") or "") or None
	doctype = str(task.get("creates_record") or "").strip()
	record_state = None
	if produced and doctype and compat.doctype_exists(doctype):
		record_state = str(frappe.db.get_value(doctype, produced, "workflow_state") or "") or None
	filed = len(doc.get("evidence_files") or [])

	data = {
		"task": _describe_task(task_row(assignment["task"])),
		"assignment": _describe_assignment(row),
		"evidence_filed": filed,
		"evidence_required": evidence_contract(task.get("evidence_required")),
		"produced_record": produced,
		"produced_record_doctype": doctype or None,
		"produced_record_state": record_state,
		"final_state": str(task.get("state") or COMPLETED),
		"x_idempotent": True,
		"completion_signature": stored,
		"visit_id": row.get("visit_id") or None,
		# v0.64.0. PRESENT AND READ BACK, NEVER RE-WRITTEN. `_flow_evidence_into_
		# shift` is not called here for the same reason nothing else in this
		# function writes: a replay is the same completion arriving twice, and
		# appending its event to the shift a second time would put one afternoon's
		# work on the timeline as two. The key is reported so a client's two code
		# paths stay one — it reads what IS on the record, which is what the first
		# call already put there.
		"shift_evidence": (
			{
				"farm_shift": row.get("farm_shift"),
				"event_logged": _shift_carries_event(str(row.get("farm_shift") or ""), assignment["name"]),
				"replayed": True,
			}
			if row.get("farm_shift")
			else None
		),
		# Null, and deliberately. A replay changed nothing, so no rule's answer
		# can have changed either — re-running the sweep here would be this app
		# doing work because a phone lost an acknowledgement.
		"compliance_evaluation": None,
		# v0.69.0. READ BACK OFF THE TASK, NEVER RE-ISSUED — the same treatment
		# `shift_evidence` gets above and for a sharper version of the same
		# reason: drawing the tank mix down a second time would take a real
		# quantity off a real shed count because a phone lost an acknowledgement.
		# The keys are present so a client's two paths stay one; both report what
		# the FIRST call already did.
		"materials_consumed": _replayed_consumption(task),
		"spray_windows": _replayed_windows(task),
		"idempotent_note": (
			f"This completion was already filed, at {row.get('completed_at')}, and nothing was "
			"changed by this call. The submission matches the one on record — same worker, same "
			"evidence, same account of the work — so this is the SAME completion arriving twice "
			"rather than a second one. That is the expected shape of a mobile client whose "
			"acknowledgement was lost to a dropped connection, and it is a success: the work is "
			"recorded, the evidence is filed"
			+ (f" and {doctype} {produced} exists" if produced else "")
			+ ". A client seeing this may clear the item from its queue."
		),
	}
	return ToolResult(
		data=data,
		summary=(
			f"{row.get('assigned_to_name') or worker} had already completed {assignment['task']} "
			f"at {row.get('completed_at')}; this identical resubmission changed nothing"
		),
		docstatus_delta=f"{COMPLETED} → {COMPLETED} (no change)",
	)


#: What a clean pass is recorded AS, on the record that inherits an empty
#: findings field. Blank findings and a blank notes field would be a record that
#: cannot tell "walked, nothing wrong" from "nobody filled this in".
CLEAN_PASS_NOTE = "No findings reported by inspector."


def clean_pass_flag(args: dict):
	"""The worker's own answer to "was this clean", as True, False, or None.

	v0.17.1. THREE STATES, AND THE THIRD IS THE POINT. None means nobody was
	asked, and the original rule applies: blank findings is a clean pass, text in
	findings opens a corrective action. That rule is right and it stays.

	It BREAKS when the evidence contract requires findings_text — as MC-Cabin-01's
	habitability inspection does — because blank is then not a submittable state,
	so a worker must type something, so every completion would open a corrective
	action against a cabin that is fine. The app therefore asks outright ("Clean
	pass" / "Issues found") and sends the answer, and Wave A treats that answer as
	AUTHORITATIVE rather than re-deriving intent by parsing the text. A worker who
	writes the words "clean pass" into a mandatory field must not trip a
	corrective action, and no amount of string-matching on findings_text is a
	sound way to avoid it.

	Accepts what Frappe and an HTTP form actually deliver: a real bool, 1/0, and
	the strings "true"/"false"/"1"/"0" that survive a JSON body reaching a
	whitelisted method as form data.
	"""
	if "clean_pass" not in args:
		return None
	raw = args.get("clean_pass")
	if raw is None or raw == "":
		return None
	if isinstance(raw, bool):
		return raw
	if isinstance(raw, (int, float)):
		return bool(raw)
	text = str(raw).strip().lower()
	if text in ("1", "true", "yes", "y"):
		return True
	if text in ("0", "false", "no", "n"):
		return False
	raise ToolError(
		f"clean_pass must be true or false, got {raw!r}. It is the worker's own answer to "
		"whether the walk was clean, and a value nobody can read is not an answer."
	)


def _marked_checklist(task: dict, args: dict) -> dict:
	"""The task's snapshotted checklist with this submission's ticks applied.

	    v0.41.0. NOTHING IS WRITTEN HERE — the caller decides whether the completion
	    survives its other refusals before any of this reaches the database, which is
	    what makes a refused completion leave the checklist exactly as the worker's
	    last successful call left it.

	    THE ARGUMENT ACCEPTS BOTH SHAPES, and the bare-string one is the reason: a
	    handset marking five items sends five names, and making it send five objects
	    to say the same thing is how a client ends up sending none. An entry may be:

	    "Press the smoke alarm"                          → done
	    {"item_name": "…", "done": true, "note": "…"}    → done, with what was found
	    {"item_name": "…", "done": false}                → explicitly NOT done

	A NAME THE TASK'S CHECKLIST DOES NOT HOLD IS REFUSED, rather than ignored. A
	typo that silently marks nothing looks exactly like a tick right up until the
	completion is refused for an item the worker believes they ticked — and the
	second refusal names a different item from the one they got wrong, which is
	the worst possible place to spend somebody's afternoon.

	TICKS ARE CUMULATIVE. An item marked done on an earlier call stays done: a
	worker who marks three items, walks out of signal, and completes later is one
	worker doing one job.
	"""
	items = checklist_items(task.get("checklist_status"))
	raw = args.get("checklist")
	if raw in (None, ""):
		return {"items": items}
	if not isinstance(raw, list):
		raise ToolError(
			"checklist must be a JSON list — either of item names, or of objects with "
			"`item_name`, `done` and an optional `note`. Nothing was changed."
		)
	if not items:
		raise ToolError(
			f"{task.get('name')} has no checklist, so there is nothing for the `checklist` "
			"argument to mark. A task raised from a Farm Task Template carrying one has its own "
			"snapshot; this task was raised without. Nothing was changed."
		)

	by_name = {str(item.get("item_name") or "").casefold(): item for item in items}
	for index, entry in enumerate(raw):
		if isinstance(entry, dict):
			name = str(entry.get("item_name") or "").strip()
			done = bool(entry.get("done", True))
			note = str(entry.get("note") or "").strip()
		else:
			name, done, note = str(entry or "").strip(), True, ""
		if not name:
			raise ToolError(f"checklist entry {index + 1} names no item. Nothing was changed.")
		item = by_name.get(name.casefold())
		if item is None:
			raise ToolError(
				f"this task's checklist has no item called {name!r}. Its items are: "
				+ ", ".join(repr(str(row.get("item_name") or "")) for row in items)
				+ ". A name that marks nothing looks exactly like a tick right up until the "
				"completion is refused for an item you believe you ticked. Nothing was changed."
			)
		# Cumulative: an explicit done=false may un-tick, but omitting an item
		# never clears a tick an earlier call made.
		item["done"] = done
		if note:
			item["note"] = note
	return {"items": items}


def _unmet_evidence(
	contract: dict,
	evidence: list,
	signature: str,
	findings_given: bool,
	witness: str,
	location_gps: str = "",
) -> list:
	"""Which of the task's evidence requirements this submission does not meet."""
	unmet = []
	types = {str(row.get("evidence_type") or "") for row in evidence}

	if contract.get("photos") and not ({"Photo", "Video"} & types):
		unmet.append(
			"photos: the task requires at least one photograph, and none was filed. Pass "
			"evidence_files as a list of File docnames from commit_staged_file, or objects with "
			'{"file": "...", "evidence_type": "Photo"}.'
		)
	if contract.get("signature") and not (signature or "Signature" in types):
		unmet.append(
			"signature: the task requires a signature capture and none was filed. Pass "
			"signature_file, or an evidence_files entry typed Signature. A signature is what turns "
			"a row somebody typed into an attestation somebody made."
		)
	if contract.get("findings_text") and not findings_given:
		unmet.append(
			"findings_text: the task requires an explicit statement of what was found. PASS AN "
			'EMPTY STRING — findings_text: "" — to record that there was nothing wrong. A clean '
			"inspection is a positive statement and that is how you make it; leaving the argument "
			"out entirely records that nobody was asked."
		)
	if contract.get("witness") and not witness:
		unmet.append(
			"witness: the task requires somebody else who was there. This is work where one "
			"person's word is not the standard, which is why the contract asked."
		)
	# v0.115.0. THE ONE REQUIREMENT NOBODY IS ASKED TO TYPE, which is precisely
	# why it has to be checked. A handset takes the fix on its own; a client that
	# never learned to send one closes the task perfectly happily and leaves a
	# season of observations that cannot be put on a map. The refusal names the
	# argument rather than the concept, because the fix is one field away.
	if contract.get("gps") and not str(location_gps or "").strip():
		unmet.append(
			"gps: the task requires a location fix and none was sent. Pass farm_location_gps as "
			'"lat,lon" — e.g. "45.5152,-122.6784". A block is twenty acres, so an observation '
			"with no coordinate cannot be compared to the next round of the same corner, put on "
			"the map, or shown to have been taken where it says it was."
		)
	return unmet


def _elapsed(started, completed):
	if not (started and completed):
		return 0
	try:
		return max(0, round(frappe.utils.time_diff_in_seconds(completed, started) / 60.0))
	except Exception:
		return 0


def _produce_record(task: dict, assignment_doc, evidence: list, signature: str, findings: str, args: dict):
	"""Build the compliance record this task promised. Returns (name, note, state, deferred).

	A task with no `creates_record` produces nothing and says nothing — most work
	is just work. A task naming a doctype this app has no builder for completes
	anyway, with the evidence filed against the assignment and a note saying what
	could not be produced: refusing the completion would strand a worker who has
	genuinely done the job in front of a tool that will not accept it.

	v0.115.0. `deferred` IS THE FOURTH ANSWER AND IT IS NOT AN ERROR. A record in
	`scouting.DEFERRED_RECORDS` is written by an idempotent SWEEP rather than
	here, for the reason `tools/lots.py` gives at length: this app installs no
	`doc_events` and `test_hooks.py` fails the build over one, so a producer an
	operator can see, switch off and re-run over last week belongs at the tool
	layer. The submitted payload comes back so `complete_farm_task` can stamp it
	onto the task, which is where the sweep reads it from — a completion whose
	Brix went nowhere would be a round somebody genuinely walked and nothing
	could later reconstruct.
	"""
	doctype = str(task.get("creates_record") or "").strip()
	if not doctype:
		return None, None, None, None

	if doctype in scouting.DEFERRED_RECORDS:
		payload = dict(_safe_json(task.get("creates_record_data")))
		payload.update(parse_json_object(args.get("record_data"), "record_data"))
		return (
			None,
			(
				f"This task produces a {doctype}, which is written by "
				f"`{scouting.DEFERRED_RECORDS[doctype]}` rather than by the completion itself — "
				"an idempotent sweep an operator can see, switch off and re-run, which is what "
				"this app has instead of document hooks. The submission is kept on the task's "
				"`creates_record_data` and the sweep reads it from there together with this "
				"assignment's location fix, photographs and findings. Run the sweep over this "
				"date to file it now; nothing is lost if it is run next week instead."
			),
			None,
			payload,
		)

	builder = inspections.BUILDERS.get(doctype)
	if builder is None:
		return (
			None,
			(
				f"This task promised a {doctype}, which erpnext_mcp has no builder for, so the "
				"evidence is filed against the assignment and no record was written. The three it "
				f"can build are: {', '.join(sorted(inspections.BUILDERS))}. The completion itself "
				"stands — refusing it would strand somebody who has done the job."
			),
			None,
			None,
		)
	if not compat.doctype_exists(doctype):
		return (
			None,
			f"This site no longer has the {doctype} DocType, so no record could be written. Run "
			"`bench migrate`. The completion and its evidence stand.",
			None,
			None,
		)

	payload = dict(_safe_json(task.get("creates_record_data")))
	payload.update(parse_json_object(args.get("record_data"), "record_data"))
	if str(task.get("location_doctype") or "") == _subject_doctype(doctype):
		# Only where the task's location is the SAME KIND of thing the record is
		# about. A `water_test_stale` alert points at a block; a Water Test is
		# about the irrigation zone that waters it, and one block can have
		# several. Filling the zone in from the block would file the sample
		# against something that is not a water source — so the worker names the
		# zone in `record_data`, and `build_water_test` refuses if they did not.
		payload.setdefault(_subject_field(doctype), task.get("location"))
	payload["source_task"] = task.get("name")
	payload["findings"] = payload.get("findings") or findings

	if clean_pass_flag(args) is True:
		# The record's findings are empty on purpose — see `complete_farm_task`.
		# The attestation goes in notes, along with whatever the worker actually
		# wrote, so the record says "walked, nothing wrong" rather than being
		# indistinguishable from one nobody filled in.
		payload["findings"] = ""
		typed = as_str(args, "findings_text").strip()
		payload["notes"] = "\n".join(
			part
			for part in (
				str(payload.get("notes") or "").strip(),
				CLEAN_PASS_NOTE,
				f"Inspector's note: {typed}" if typed else "",
			)
			if part
		)
	payload.setdefault(_person_field(doctype), assignment_doc.assigned_to)
	payload.setdefault(f"{_person_field(doctype)}_name", assignment_doc.assigned_to_name)
	if doctype == inspections.HOUSING_INSPECTION and signature:
		payload.setdefault("signature", signature)

	record = builder(payload, evidence)
	return record.name, None, str(record.workflow_state or ""), None


def _subject_field(doctype: str) -> str:
	spec = inspections.SPECS.get(doctype)
	return spec.subject_field if spec else "unit"


def _subject_doctype(doctype: str) -> str:
	spec = inspections.SPECS.get(doctype)
	return spec.subject_doctype if spec else ""


def _person_field(doctype: str) -> str:
	spec = inspections.SPECS.get(doctype)
	return spec.person_field if spec else "inspector"


# ── 6. reject_farm_task ─────────────────────────────────────────────────────
def reject_farm_task(args: dict) -> ToolResult:
	"""Hand one task back, with a reason. Rejection is a first-class state."""
	_require()
	assignment = _assignment_for(args)
	worker = _worker(args, required=False)
	if worker and assignment.get("assigned_to") != worker:
		raise ToolError(
			f"{assignment['task']} is held by {assignment.get('assigned_to_name')}, not {worker}. "
			"Nothing was changed."
		)
	# A PAUSED TASK CAN BE HANDED BACK. That is the honest end of an interruption
	# somebody never got back to — "I was called away and the ladder is still
	# broken" is the sentence a board needs, and forcing a resume first to reject
	# it would put a minute of clock on a job nobody touched.
	if assignment.get("state") not in (CLAIMED, IN_PROGRESS, PAUSED):
		raise ToolError(
			f"{assignment['name']} is {assignment.get('state')} and cannot be rejected. Nothing was changed."
		)

	reason = as_str(args, "reason") or as_str(args, "rejection_reason")
	if not reason:
		raise ToolError(
			"reason is required. This is the most useful sentence in the whole doctype: it is what "
			"turns 'nobody got to it and dispatch never followed up' — the answer nobody can defend "
			"— into 'the ladder is broken and I could not reach the detector', which is a fact "
			"somebody can act on. Nothing was changed."
		)

	doc = frappe.get_doc(FARM_TASK_ASSIGNMENT, assignment["name"])
	doc.state = REJECTED
	doc.rejection_reason = reason
	doc.completed_at = frappe.utils.now()
	# The minutes somebody DID spend before handing it back are kept. A rejection
	# is not a claim that no work happened — it is a claim that the work could
	# not be finished, and the half hour spent finding that out was still worked.
	_close_segment(doc, str(doc.completed_at), SEGMENT_REJECTION, reason)
	doc.paused_at = None
	doc.actual_duration_minutes = active_minutes(doc)
	doc.save(ignore_permissions=True)

	# Back to the pool, with the name cleared. The rejected assignment stays: it
	# is the record that somebody WAS sent, looked, and could not do it, which is
	# a considerably better compliance answer than an absence.
	back_to = AVAILABLE if not as_bool(args, "cancel", False) else CANCELLED
	_set_task_state(assignment["task"], back_to, assigned_to="", assigned_to_name="")

	return ToolResult(
		data={
			"task": _describe_task(task_row(assignment["task"])),
			"assignment": _describe_assignment(dict(doc.as_dict())),
			"returned_to_state": back_to,
			"note": (
				"The rejected assignment stays on the record. It is the proof that somebody was "
				"sent, went, and could not do it — which answers an auditor's question in a way "
				"that an absence never does. The task is back "
				+ ("in the pool." if back_to == AVAILABLE else "and cancelled.")
			),
		},
		summary=f"{doc.assigned_to_name} rejected {assignment['task']}: {reason[:80]}",
		docstatus_delta=f"{assignment.get('state')} → {REJECTED}",
	)


# ── 7. list_available_tasks ─────────────────────────────────────────────────
def list_available_tasks(args: dict) -> ToolResult:
	"""The pool: what a worker could pick up right now, and whether they may."""
	_require()
	company = _company(args)
	limit = min(as_limit(args), BOARD_CAP)

	filters = {"state": AVAILABLE, "dispatch_mode": ("in", list(SELF_PICKABLE))}
	if company:
		filters["company"] = company
	for key, fieldname in (("location", "location"), ("skill", "skill_required"), ("task_type", "task_type")):
		value = as_str(args, key) or as_str(args, fieldname)
		if value:
			filters[fieldname] = value
	urgency = as_str(args, "urgency")
	if urgency:
		filters["urgency"] = as_choice(FARM_TASK, "urgency", urgency, "urgency")

	rows = frappe.db.get_all(
		FARM_TASK,
		filters=filters,
		fields=compat.existing_fields(FARM_TASK, _TASK_FIELDS),
		order_by="modified desc",
		limit=limit,
	)
	tasks = _by_urgency([_describe_task(dict(row)) for row in rows or []])

	worker = as_str(args, "worker_id")
	data = {
		"company": company,
		"count": len(tasks),
		"limit": limit,
		"truncated": len(tasks) >= limit,
		"tasks": tasks,
		"filters": {
			"location": as_str(args, "location") or None,
			"skill": as_str(args, "skill") or as_str(args, "skill_required") or None,
			"task_type": as_str(args, "task_type") or None,
			"urgency": urgency or None,
		},
		"note": (
			"Only Self-pick and Either tasks are here. Dispatched work is deliberately absent from "
			"the pool: somebody has to be SENT to it by name, because that is how this app marks "
			"work where the named holder matters."
		),
	}
	if worker:
		held = concurrent_claims(worker)
		data["worker_id"] = worker
		data["concurrent_claims"] = len(held)
		data["claims_remaining"] = max(0, MAX_CONCURRENT_CLAIMS - len(held))
		data["may_claim"] = len(held) < MAX_CONCURRENT_CLAIMS
		if not data["may_claim"]:
			data["claim_note"] = (
				f"{worker} is holding {len(held)} task(s), which is the limit of "
				f"{MAX_CONCURRENT_CLAIMS}. Completing or rejecting one frees a slot immediately."
			)
	return ToolResult(
		data=data,
		summary=f"{len(tasks)} task(s) in the pool"
		+ (f"; {worker} may claim {data.get('claims_remaining')}" if worker else ""),
	)


def _by_urgency(tasks: list) -> list:
	order = {"Critical": 0, "High": 1, "Normal": 2, "Low": 3}
	return sorted(tasks, key=lambda task: (order.get(task["urgency"], 9), str(task["name"])))


# ── 8. list_dispatched_tasks ────────────────────────────────────────────────
def list_dispatched_tasks(args: dict) -> ToolResult:
	"""Everything one worker is holding or has finished, with their assignments."""
	_require()
	worker = _worker(args, required=True)
	limit = min(as_limit(args), BOARD_CAP)

	filters = {"assigned_to": worker}
	state = as_str(args, "state")
	if state:
		filters["state"] = as_choice(FARM_TASK_ASSIGNMENT, "state", state, "state")
	elif not as_bool(args, "include_finished", False):
		filters["state"] = ("in", [CLAIMED, IN_PROGRESS])
	company = _company(args)
	if company:
		filters["company"] = company

	rows = frappe.db.get_all(
		FARM_TASK_ASSIGNMENT,
		filters=filters,
		fields=compat.existing_fields(FARM_TASK_ASSIGNMENT, _ASSIGNMENT_FIELDS),
		order_by="creation desc",
		limit=limit,
	)
	assignments = [_describe_assignment(dict(row)) for row in rows or []]
	task_names = sorted({entry["task"] for entry in assignments if entry.get("task")})
	tasks = {}
	if task_names:
		for row in (
			frappe.db.get_all(
				FARM_TASK,
				filters={"name": ("in", task_names)},
				fields=compat.existing_fields(FARM_TASK, _TASK_FIELDS),
				limit=BOARD_CAP,
			)
			or []
		):
			tasks[row["name"]] = _describe_task(dict(row))
	for entry in assignments:
		entry["task_detail"] = tasks.get(entry.get("task"))

	held = [entry for entry in assignments if entry["state"] in (CLAIMED, IN_PROGRESS)]
	return ToolResult(
		data={
			"worker_id": worker,
			"count": len(assignments),
			"holding_now": len(held),
			"claims_remaining": max(0, MAX_CONCURRENT_CLAIMS - len(concurrent_claims(worker))),
			"limit": limit,
			"truncated": len(assignments) >= limit,
			"assignments": assignments,
		},
		summary=f"{worker} has {len(held)} task(s) in hand, {len(assignments)} shown",
	)


# ── 9. list_dispatch_board ──────────────────────────────────────────────────
def list_dispatch_board(args: dict) -> ToolResult:
	"""The Kanban as JSON: every task grouped by state, worst urgency first."""
	_require()
	company = _company(args)
	limit = min(as_limit(args), BOARD_CAP)

	filters = {}
	if company:
		filters["company"] = company
	state_filter = as_str(args, "state_filter") or as_str(args, "state")
	if state_filter:
		wanted = [part.strip() for part in state_filter.split(",") if part.strip()]
		unknown = [part for part in wanted if part not in STATES]
		if unknown:
			raise ToolError(
				f"state_filter names {', '.join(repr(part) for part in unknown)}, which is not a Farm "
				f"Task state. The eight are: {', '.join(STATES)}."
			)
		filters["state"] = ("in", wanted)
	elif not as_bool(args, "include_closed", False):
		filters["state"] = ("in", [state for state in STATES if state not in TERMINAL_STATES])
	for key in ("task_type", "urgency", "assigned_to", "skill_required"):
		value = as_str(args, key)
		if value:
			filters[key] = value
	# v0.64.0. THE BOARD, NARROWED TO ONE SHIFT. "What is still open on the crew
	# that is out there right now" is the question a foreman actually asks at
	# two in the afternoon, and answering it by reading the whole board and
	# filtering by eye is how the one Critical job gets missed.
	board_shift = _shift_argument(args, company or "")
	if board_shift:
		filters["farm_shift"] = board_shift

	rows = frappe.db.get_all(
		FARM_TASK,
		filters=filters,
		fields=compat.existing_fields(FARM_TASK, _TASK_FIELDS),
		order_by="modified desc",
		limit=limit,
	)
	tasks = _by_urgency([_describe_task(dict(row)) for row in rows or []])

	columns = {state: [] for state in STATES}
	for task in tasks:
		columns.setdefault(task["state"], []).append(task)
	from_alerts = [task for task in tasks if task["source_alert"]]
	unassigned = [task["name"] for task in tasks if task["state"] == AVAILABLE]
	critical = [task["name"] for task in tasks if task["urgency"] == "Critical" and task["open"]]

	# v0.64.0. WHICH SHIFTS THIS BOARD'S WORK BELONGS TO, and how much of it
	# belongs to none. The second number is the one worth reading: an unanchored
	# task's completion evidence reaches no compliance record spanning an
	# exposure period, so a board that is mostly unanchored is a dispatch surface
	# that is not feeding the shift record it was built to feed.
	by_shift = {}
	for task in tasks:
		shift = task.get("farm_shift")
		if shift:
			by_shift.setdefault(shift, []).append(task["name"])
	unanchored = [task["name"] for task in tasks if not task.get("farm_shift")]

	data = {
		"company": company,
		"farm_shift": board_shift or None,
		"count": len(tasks),
		"limit": limit,
		"truncated": len(tasks) >= limit,
		"by_state": {state: len(entries) for state, entries in columns.items()},
		"columns": {state: entries for state, entries in columns.items() if entries},
		"in_the_pool": unassigned,
		"open_critical": critical,
		"by_shift": by_shift,
		"not_anchored_to_a_shift": unanchored,
		"generated_from_alerts": len(from_alerts),
		"kanban_route": "/app/farm-task/view/kanban/Farm Task Dispatch",
		"note": (
			f"{len(from_alerts)} of {len(tasks)} task(s) on this board came from a compliance "
			"alert. That fraction is the honest measure of whether the calendar is driving "
			"work or being read and ignored."
		),
	}
	if unanchored and tasks:
		data["shift_note"] = (
			f"{len(unanchored)} of {len(tasks)} task(s) here name no shift. Their completions file "
			"evidence against the assignment and reach no record spanning an exposure period — "
			"which is correct for desk work and a gap for anything done by a crew in a field. "
			"farm_shift can be set at creation, dispatch, clock-in or completion, and clock-in "
			"infers it where the worker is rostered on exactly one open shift."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{len(tasks)} task(s) on the board: {len(unassigned)} in the pool, {len(critical)} open Critical"
			+ (f", across {len(by_shift)} shift(s)" if by_shift else "")
		),
	)


# ── 10. get_farm_task ───────────────────────────────────────────────────────
def get_farm_task(args: dict) -> ToolResult:
	"""One task in full, with every assignment it has ever had and their evidence."""
	_require()
	row = task_row(as_str(args, "task", required=True))
	described = _describe_task(row)

	history = [
		_describe_assignment(dict(entry))
		for entry in frappe.db.get_all(
			FARM_TASK_ASSIGNMENT,
			filters={"task": row["name"]},
			fields=compat.existing_fields(FARM_TASK_ASSIGNMENT, _ASSIGNMENT_FIELDS),
			order_by="creation desc",
			limit=100,
		)
		or []
	]
	for entry in history:
		entry["evidence"] = _assignment_evidence(entry["name"])

	described["assignments"] = history
	described["assignment_count"] = len(history)
	described["rejections"] = [entry for entry in history if entry["state"] == REJECTED]
	described["live_assignment"] = next(
		(entry for entry in history if entry["state"] in (CLAIMED, IN_PROGRESS)), None
	)

	if row.get("source_alert") and compat.doctype_exists(ALERT):
		alert = frappe.db.get_value(
			ALERT,
			row["source_alert"],
			["alert_type", "severity", "dismissed", "auto_dismissed", "alert_message", "due_date"],
			as_dict=True,
		)
		described["alert"] = dict(alert) if alert else None
		if alert and compat.checked(alert.get("auto_dismissed")):
			described["loop_closed"] = (
				"The alert this task came from has auto-dismissed, which means the sweep found its "
				"condition no longer true. The work was done and the calendar noticed by itself."
			)
	return ToolResult(
		data=described,
		summary=(
			f"{row['name']} — {described['task_name']} ({described['state']}, {len(history)} assignment(s))"
		),
	)


def _assignment_evidence(assignment: str) -> list:
	if not compat.doctype_exists(inspections.EVIDENCE_CHILD):
		return []
	rows = frappe.db.get_all(
		inspections.EVIDENCE_CHILD,
		filters={"parent": assignment, "parenttype": FARM_TASK_ASSIGNMENT},
		fields=[
			"evidence_type",
			"file",
			"file_url",
			"caption",
			"captured_on",
			# v0.96.0. Read explicitly, like every other column here: the row is
			# no use to a reader who has to guess which frame is the before one
			# off a filename, which is the whole reason the column was added.
			*compat.existing_fields(inspections.EVIDENCE_CHILD, ("phase",)),
			"idx",
		],
		order_by="idx asc",
		limit=inspections.EVIDENCE_CAP * 2,
	)
	return [
		{
			"evidence_type": row.get("evidence_type"),
			"file": row.get("file"),
			"file_url": row.get("file_url"),
			"caption": row.get("caption"),
			"captured_on": str(row.get("captured_on") or "") or None,
			# `None` rather than absent, so a client reading a list of frames
			# gets the same keys on every one of them and an untagged photograph
			# is visibly untagged rather than missing a field.
			"phase": row.get("phase") or None,
		}
		for row in rows or []
	]


# ── 11. generate_tasks_from_compliance_alerts ───────────────────────────────
#: v0.55.0. The named routings a recipe may ask for, as constants rather than
#: string literals in two files. A rule record spells the same names in
#: `extra_parameters.producer_assignee_resolver`, and a typo there is refused by
#: `_assignee_from_resolver` with the list — not silently routed to nobody.
RESOLVER_SUPERVISOR = "employee_supervisor"
RESOLVER_SIGNER = "i9_authorized_signer"


def _employee_of_source(row: dict) -> str:
	"""The Employee an alert's source record is about, or "".

	Every doctype the resolvers below run against — I-9 Form, W-4 Form — carries
	a Link column called `employee`, because both are records ABOUT one person.
	Read by name rather than configured on the rule: a doctype where the column
	is called something else is a doctype these resolvers do not serve, and
	saying so by answering "" is better than a configurable field that lets
	somebody point a signature rule at a Purchase Order.
	"""
	return str(row.get("employee") or "").strip()


def _resolve_employee_supervisor(row: dict, notes: list) -> str:
	"""Whoever the subject employee reports to. For the signatures THEY have to give.

	SECTION 1 AND THE W-4 ARE SIGNED BY THE EMPLOYEE, so the work is not signing
	— it is FINDING somebody who is out on a crew somewhere and putting a phone
	in front of them. Nobody at a desk can do that; the person who can is whoever
	already knows which block that crew is on this morning, which is what
	`reports_to` records.

	ANSWERS "" RATHER THAN GUESSING, in all four of the ways this can fail: no
	employee on the record, no Employee doctype on the site, no `reports_to`
	column, or a `reports_to` nobody filled in. Each leaves the task on its skill
	pool and says which one it was on the report — a task assigned to a name
	nobody holds is off the pool AND on nobody's list.
	"""
	employee = _employee_of_source(row)
	if not employee:
		return ""
	if not compat.doctype_exists(EMPLOYEE) or not compat.has_field(EMPLOYEE, "reports_to"):
		notes.append(
			"this site's Employee register has no reports_to column, so there is no supervisor "
			"to route the signature to. The task routes by skill."
		)
		return ""
	supervisor = str(frappe.db.get_value(EMPLOYEE, employee, "reports_to") or "").strip()
	if not supervisor:
		notes.append(
			f"{employee} has no reports_to on their Employee record, so this app does not know "
			"who would go and find them. The task routes by skill."
		)
	return supervisor


def _resolve_authorized_signer(row: dict, notes: list) -> str:
	"""An authorized signer who can be sent to sign the employer's half.

	THE ROSTER IS THE ANSWER TO A LEGAL QUESTION, NOT A CONVENIENCE. `tools/
	signers.py` holds who this employer has authorised to complete Section 2 of
	an I-9 and the employer block of a W-4, and only those accounts may. Routing
	this task to anybody else would be raising work that its holder is not
	permitted to complete — the sign call would refuse them, having already put
	the task on their list.

	WHOEVER ALREADY VERIFIED THE DOCUMENTS COMES FIRST. An I-9 missing its
	Section 2 signature usually names its `verifier_name` — somebody examined the
	documents and the signature is what did not get captured — and sending the
	task to that person asks them to finish what they started rather than asking
	a colleague to attest to an examination they were not present for. §274a.2
	makes that distinction, not this app: the attestation is that *the signer*
	examined the documents.

	AN EMPTY ROSTER MEANS UNRESTRICTED, which is every site that has not run
	`add_authorized_signer`, and there it answers "" — the task falls to the
	`hr_admin` pool, which is what those sites got for their whole life before
	this release. A roster with nobody mappable to an Employee does the same and
	says so.
	"""
	from . import signers

	try:
		roster = [dict(entry) for entry in signers._rows()]
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		notes.append(f"the authorized-signer roster could not be read ({exc}); the task routes by skill.")
		return ""
	live = [
		entry
		for entry in roster
		if compat.checked(entry.get("active")) and compat.checked(entry.get("can_sign_i9"))
	]
	if not live:
		notes.append(
			"no active signer on this site is authorised to sign an I-9 — an empty roster means "
			"unrestricted, so the task routes to the hr_admin pool rather than to a name. "
			"add_authorized_signer is what turns this into a named routing."
		)
		return ""

	verifier = str(row.get("verifier_name") or "").strip().casefold()
	ordered = sorted(live, key=lambda entry: str(entry.get("full_name") or "").strip().casefold() != verifier)
	for entry in ordered:
		employee = _employee_for_user(str(entry.get("user") or ""))
		if employee:
			return employee
	notes.append(
		"every active I-9 signer on the roster is a User with no Employee record, and a Farm "
		"Task is held by an Employee. Link their accounts with the user_id column on Employee, "
		"or the task stays on the hr_admin pool."
	)
	return ""


def _employee_for_user(user: str) -> str:
	"""The Employee docname behind one User account, or "". Never raises."""
	user = str(user or "").strip()
	if not (user and compat.doctype_exists(EMPLOYEE) and compat.has_field(EMPLOYEE, "user_id")):
		return ""
	try:
		return str(frappe.db.get_value(EMPLOYEE, {"user_id": user}, "name") or "").strip()
	except Exception:  # pragma: no cover - a site mid-migrate
		return ""


#: Resolver name → the function. A CLOSED REGISTRY, for the reason
#: `alerts/rules.SCANNERS` is one: "work out who to send this to" is one sentence
#: away from "run this against the database", and a rule record naming something
#: that is not in here routes to the pool with a sentence saying so rather than
#: to nobody, quietly.
ASSIGNEE_RESOLVERS = {
	RESOLVER_SUPERVISOR: _resolve_employee_supervisor,
	RESOLVER_SIGNER: _resolve_authorized_signer,
}

#: What each alert type becomes when it stops being a warning and starts being
#: work. Every entry is a judgement about the SHAPE of the job, and the two that
#: matter most are `dispatch` and `evidence`:
#:
#:   * SELF-PICK for general labour. Fifty-four habitability walks that a foreman
#:     has to assign by hand are fifty-four walks that do not happen.
#:   * DISPATCHED wherever the named holder matters — a licence renewal, an I-9,
#:     anything an agency will later ask "who did this" about.
#:
#: An alert type absent from this table is reported by name rather than turned
#: into a generic task. A task with a made-up evidence contract is worse than no
#: task: it produces a compliance record nobody can rely on.
ALERT_TASK_MAP = {
	"housing_inspection_overdue": {
		"task_type": "Inspection",
		"creates_record": "Housing Inspection",
		"skill": "camp_maintenance",
		"dispatch": "Self-pick",
		"minutes": 45,
		"evidence": {"photos": True, "signature": True, "findings_text": True},
		"what": "Walk the cabin and record a habitability inspection",
	},
	"housing_detector_test_stale": {
		"task_type": "Test",
		"creates_record": "Detector Test",
		"skill": "camp_maintenance",
		"dispatch": "Self-pick",
		"minutes": 20,
		"evidence": {"photos": True, "findings_text": True},
		"what": "Test the smoke and CO detectors and record the result",
	},
	"water_test_stale": {
		"task_type": "Water-Sampling",
		"creates_record": "Water Test",
		"skill": "water_sampling",
		"dispatch": "Self-pick",
		"minutes": 60,
		"evidence": {"photos": True, "findings_text": True},
		"what": "Take an agricultural water sample and send it to the laboratory",
	},
	"certification_expiring": {
		"task_type": "Compliance-Audit",
		"creates_record": "",
		"skill": "compliance_admin",
		"dispatch": "Dispatched",
		"minutes": 90,
		"evidence": {"findings_text": True},
		"what": "Renew the certificate before it lapses",
	},
	"policy_review_overdue": {
		"task_type": "Compliance-Audit",
		"creates_record": "",
		"skill": "compliance_admin",
		"dispatch": "Dispatched",
		"minutes": 120,
		"evidence": {"findings_text": True},
		"what": "Review the procedure and record what changed",
	},
	"i9_expired": {
		"task_type": "Compliance-Audit",
		"creates_record": "",
		"skill": "hr_admin",
		"dispatch": "Dispatched",
		"minutes": 30,
		"evidence": {"findings_text": True, "signature": True},
		"what": "Re-verify employment authorisation",
	},
	"flc_license_expiring": {
		"task_type": "Compliance-Audit",
		"creates_record": "",
		"skill": "hr_admin",
		"dispatch": "Dispatched",
		"minutes": 60,
		"evidence": {"findings_text": True},
		"what": "Renew the farm labor contractor licence",
	},
	"filing_response_due": {
		"task_type": "Compliance-Audit",
		"creates_record": "",
		"skill": "compliance_admin",
		"dispatch": "Dispatched",
		"minutes": 45,
		"evidence": {"findings_text": True},
		"what": "Chase the agency for a response before the deadline",
	},
	"audit_action_overdue": {
		"task_type": "Compliance-Audit",
		"creates_record": "",
		"skill": "compliance_admin",
		"dispatch": "Dispatched",
		"minutes": 120,
		"evidence": {"findings_text": True, "photos": True},
		"what": "Close the corrective action the auditor raised",
	},
	"housing_corrective_action_open": {
		"task_type": "Repair",
		"creates_record": "",
		"skill": "camp_maintenance",
		"dispatch": "Dispatched",
		"minutes": 90,
		"evidence": {"photos": True, "findings_text": True},
		"what": "Fix what the inspection found, then record a fresh one",
	},
	"water_test_contamination": {
		"task_type": "Water-Sampling",
		"creates_record": "Water Test",
		"skill": "water_sampling",
		"dispatch": "Dispatched",
		"minutes": 90,
		"evidence": {"photos": True, "findings_text": True},
		"what": "Treat or switch the source, then re-sample",
	},
	# v0.19.0. `creates_record` is deliberately EMPTY even though there is an
	# Employee Training Record doctype now. Completing this task is arranging and
	# delivering a retraining — a trainer, a crew, a language, a room — and the
	# record it produces has to name the topics actually covered and carry the
	# trainee's own signature. `complete_farm_task` has no builder that can invent
	# either, and a task that auto-filed a training record with no topics and no
	# signature would produce exactly the document an auditor disallows. So the
	# task closes with findings text, and `record_training` files the evidence.
	"training_expiring": {
		"task_type": "Compliance-Audit",
		"creates_record": "",
		"skill": "hr_admin",
		"dispatch": "Dispatched",
		"minutes": 120,
		"evidence": {"findings_text": True, "signature": True},
		"what": "Arrange and deliver the retraining, then file it with record_training",
	},
	# v0.19.3. Fifteen minutes, and it is a real fifteen minutes: the work is a
	# supervisor reading a record they were not present for and putting their name
	# to it. `creates_record` is empty for the same reason the training entry's is
	# — the record already exists, and what is missing is a signature on it, which
	# `sign_training_supervisor_review` writes and no completion builder can
	# invent. The evidence contract asks for the signature itself rather than a
	# findings note, because a review with no signature is the gap this task was
	# raised to close.
	"supervisor_review_lapsed": {
		"task_type": "Compliance-Audit",
		"creates_record": "",
		"skill": "hr_admin",
		"dispatch": "Dispatched",
		"minutes": 15,
		"evidence": {"signature": True},
		"what": (
			"Read the record and sign the §112.161(b) supervisor review with sign_training_supervisor_review"
		),
	},
	# ── v0.55.0: the first recipe that names a person without naming a column ──
	#
	# Every entry above either routes to a POOL by skill or, on the rules that
	# carry `producer_assigned_to_expression`, to whoever a column on the tripped
	# row happens to hold — `row.foreman`, and the foreman is on the shift.
	# Neither reaches an unsigned reverification. The person who has to sign it
	# is not on the I-9; they are on the authorized-signer roster, which is a
	# different doctype an expression cannot walk to.
	#
	# So this names a RESOLVER — see `ASSIGNEE_RESOLVERS` — which is a reviewed,
	# shipped function taking the alert's source row and answering with an
	# Employee docname or nothing. It is the same trade `builtin_scanner` makes
	# on the rule side: the SHAPE of the lookup is code because it walks doctypes
	# an expression cannot reach.
	#
	# `subject` IS WHAT MAKES THE TASK READ LIKE AN ERRAND rather than a docname.
	# "Collect I-9 Supplement B signature for Juan Lopez" is a sentence a foreman
	# can act on; "… — I9-2026-0043" is a lookup they have to do first.
	#
	# ITS THREE SIBLINGS ARE NOT IN THIS TABLE, and their absence is the design
	# rather than an omission. `i9_section_1_unsigned`, `i9_section_2_unsigned`
	# and `w4_signature_missing` are RECORD-ONLY rules — authored in the
	# declarative vocabulary, with no `Rule` object in `alerts/rules.py` to fall
	# back to — so their producer recipe lives on the record too, in
	# `producer_farm_task_type`, `evidence_contract` and `extra_parameters`, and
	# is read by the third path in `_recipe_for`. Copying it up here would put
	# the definition of an editable rule in a table nobody can edit, and this
	# table would silently win. This one has a scanner and a `shape(...)`, so it
	# migrates the way every built-in does: through here.
	"i9_supplement_b_unsigned": {
		# COMPLIANCE-AUDIT RATHER THAN HIRING, and it is the one of the four that
		# differs. Sections 1 and 2 are onboarding: they happen once, in the first
		# three days, and belong on the hiring board with the rest of that
		# afternoon's paperwork. A Supplement B is a REVERIFICATION — it happens
		# to somebody who has worked here for a season or five, when a document
		# expires or they are rehired — and filing it under Hiring would put a
		# returning tractor driver on a new-hire board.
		"task_type": "Compliance-Audit",
		"creates_record": "",
		"skill": "hr_admin",
		"dispatch": "Dispatched",
		"minutes": 20,
		"evidence": {"signature": True},
		"what": "Collect I-9 Supplement B signature",
		"assignee_resolver": RESOLVER_SIGNER,
		"subject": "employee_name",
	},
}


def _rule_row_for(alert_type: str) -> dict:
	"""The live Compliance Rule behind one alert type, or {}. Never raises."""
	try:
		from .. import compliance_rules

		name = compliance_rules.resolve(alert_type)
		return compliance_rules.rule_row(name) if name else {}
	except Exception:  # pragma: no cover - a site mid-migrate
		return {}


def producer_templates() -> dict:
	"""alert type → the live rule row that names a Farm Task Template. v0.41.0.

	ONE QUERY FOR THE WHOLE SWEEP, and that is why it exists rather than
	`_recipe_for` asking per alert. `generate_tasks_from_compliance_alerts` walks
	up to five hundred alerts; resolving each one's rule and reading its row would
	be four queries an alert for a fact almost every alert does not have — no rule
	this app seeds names a producer template, so on an ordinary site this returns
	an empty dict off one query and the whole template path costs nothing.

	Only ENABLED, UNSUPERSEDED rows are read. A rule somebody switched off should
	not be quietly deciding the shape of the work a different rule asks for, and a
	superseded row is a definition that has already been replaced.
	"""
	if not compat.doctype_exists("Compliance Rule"):
		return {}
	try:
		rows = frappe.db.get_all(
			"Compliance Rule",
			filters={
				"producer_task_template": ("not in", ("", None)),
				"enabled": 1,
				"superseded_by": ("in", ("", None)),
			},
			fields=[
				"name",
				"rule_id",
				"title",
				"producer_task_template",
				"producer_assigned_to_expression",
			],
			limit=500,
		)
	except Exception:  # pragma: no cover - a site mid-migrate
		return {}
	return {str(row["rule_id"]): dict(row) for row in rows or [] if row.get("rule_id")}


def _recipe_from_template(template: str, row: dict) -> dict | None:
	"""The shape of work one Farm Task Template defines. v0.41.0.

	THE TEMPLATE IS THE WHOLE RECIPE where a rule names one — the type, the skill,
	the minutes, the dispatch mode, the evidence contract, the record it produces
	and the checklist — and the rule's inline `producer_*` fields are not
	consulted at all. That is the point of the release: two rules asking for the
	same job state it once, in one record, instead of twice in full and drifting.

	The rule still owns TWO things the template cannot know, and both are facts
	about the RULE rather than about the job: `producer_assigned_to_expression`,
	because who gets sent depends on the row that tripped, and the alert's own
	message, which becomes the task's case-specific note. Everything else comes
	off the template.

	Returns None where the template has gone missing or been disabled, and None
	falls the caller through to the ordinary paths — a rule pointing at a deleted
	template raising a table-shaped task is better than a rule raising nothing.
	"""
	from .. import task_templates

	resolved = task_templates.resolve(template)
	if not resolved:
		return None
	shape = task_templates.snapshot(resolved)
	if not shape or not compat.checked(task_templates.template_row(resolved).get("enabled")):
		return None
	expression = str(row.get("producer_assigned_to_expression") or "").strip()
	return {
		"task_type": shape["task_type"],
		"creates_record": shape["creates_record"],
		"skill": shape["skill_required"],
		# A named holder wins over the template's own mode, exactly as it does on
		# the table path: `Dispatched` is this app's word for "somebody has to be
		# SENT to this by name", and a task with a named holder is not in a pool.
		"dispatch": DISPATCH_DISPATCHED if expression else shape["dispatch_mode"],
		"minutes": shape["estimated_duration_minutes"],
		"evidence": dict(shape["evidence_required"]),
		"what": str(row.get("title") or "").strip() or shape["template"],
		"assigned_to_expression": expression,
		"template": shape["template"],
		"checklist_status": shape["checklist_status"],
		"creates_record_data": dict(shape["creates_record_data"]),
		"instructions": shape["notes"],
	}


def has_task_recipe(alert_type: str) -> bool:
	"""Whether an alert type has ANY shape of task this app knows how to raise.

	The public half of `_recipe_for`, for a caller that only needs the yes/no —
	`api/rectify.py` reads it to decide whether "raise a task" is an honest thing
	to offer a phone for an alert type it has no more specific answer for.
	"""
	return _recipe_for(alert_type) is not None


def _recipe_for(alert_type: str, producers: dict | None = None) -> dict | None:
	"""The shape of work one alert type becomes: the TEMPLATE, then the table, then the record.

	v0.22.5 added the second and third of those. v0.41.0 added the first, and the
	ordering is the whole of what changed.

	1. **THE RULE'S `producer_task_template`, WHERE IT HAS ONE.** A Farm Task
	   Template is an explicit statement, by whoever authored the rule, that this
	   work has a defined shape somewhere else — and a statement that specific has
	   to outrank a table this app wrote before the operation existed. It is also
	   how the seeded templates become useful to the shipped rules: point
	   `housing_inspection_overdue` at "Cabin Habitability Inspection" and the
	   generated task carries the checklist.

	   THIS DOES NOT BREAK THE BACKWARD-COMPATIBILITY GUARANTEE BELOW, and the
	   reason is worth stating: no rule this app seeds has a producer template, so
	   nothing moves until somebody deliberately points a rule at one. The five
	   seeded templates match `ALERT_TASK_MAP` field for field on purpose — same
	   type, same skill, same minutes, same dispatch mode, same evidence contract
	   — so even after the wiring the task is the task it always was, plus a
	   checklist. `test_task_templates.py` asserts that equality so the day
	   somebody edits one table the other cannot quietly stay behind.

	2. **`ALERT_TASK_MAP`.** The thirteen shipped rules produce exactly the tasks
	   they produced in v0.22.1, out of the same reviewed table, whatever a site
	   has since edited onto their records.

	3. **THE RULE'S OWN INLINE `producer_*` FIELDS.** The case the table cannot
	   cover, because the rule did not exist when it was written. Since v0.22.0
	   every Compliance Rule has carried `producer_farm_task_type`,
	   `producer_skill_required` and `evidence_contract`; until v0.22.5 nothing
	   read them back, so a rule authored after the framework shipped had a
	   producer recipe on its record and landed in `skipped_unmapped` anyway.

	`producers` is the map `producer_templates()` builds once per sweep, so five
	hundred alerts cost one query for step 1 rather than four apiece. Omitting it
	is correct for a one-off caller and simply asks for the map here.

	Returns None where none of the three has anything to say, and None still means
	"reported by name, not turned into a generic task".
	"""
	if producers is None:
		producers = producer_templates()

	producer = producers.get(alert_type) or {}
	template = str(producer.get("producer_task_template") or "").strip()
	if template:
		from_template = _recipe_from_template(template, producer)
		if from_template is not None:
			return from_template

	recipe = ALERT_TASK_MAP.get(alert_type)
	if recipe is not None:
		return recipe

	row = _rule_row_for(alert_type)
	if not row:
		return None

	# Reaching here means `_rule_row_for` imported the module and read a row, so
	# this import cannot be the thing that fails on a site mid-migrate.
	from .. import compliance_rules

	task_type = str(row.get("producer_farm_task_type") or "").strip()
	evidence = compliance_rules._quietly(
		compliance_rules.parse_contract, row.get("evidence_contract_json"), {}
	)
	if not (task_type and evidence):
		# A TASK WITH NO EVIDENCE CONTRACT IS A TASK SOMEBODY CLOSES WITH A TICK,
		# and a tick in a box is what an auditor is trained to disbelieve. Both
		# halves are required before an alert becomes work, exactly as they are for
		# every entry in the table above.
		return None

	extra = compliance_rules._quietly(compliance_rules.as_object, row.get("extra_parameters_json"), {})
	expression = str(row.get("producer_assigned_to_expression") or "").strip()
	resolver = str(extra.get("producer_assignee_resolver") or "").strip()
	return {
		"task_type": task_type,
		"creates_record": "",
		"skill": str(row.get("producer_skill_required") or "").strip(),
		# ROUTED BY NAME WHERE THERE IS A NAME TO ROUTE TO. `Dispatched` is this
		# app's existing word for "somebody has to be SENT to this by name" — the
		# same mode a licence renewal and an I-9 re-verification take — and a
		# fourth dispatch_mode meaning the same thing would be a second vocabulary
		# for one idea, on the enum the iOS build switches on.
		#
		# v0.55.0 ADDED THE SECOND WAY TO HAVE A NAME. A resolver names a person
		# the tripped row does not mention — the employee's supervisor, an
		# authorized signer — and a rule using one is as much a "send somebody"
		# rule as one carrying an expression. `_pool_dispatch` catches the case
		# where the lookup then finds nobody.
		"dispatch": DISPATCH_DISPATCHED if (expression or resolver) else "Self-pick",
		"minutes": int(extra.get("producer_task_minutes") or 0),
		"evidence": dict(evidence),
		"what": str(extra.get("producer_task_what") or "").strip() or str(row.get("title") or alert_type),
		"assigned_to_expression": expression,
		# v0.55.0. The two the record can state that the expression vocabulary
		# cannot: WHO by named lookup rather than by column, and what to CALL the
		# task's subject. Both are read here so a rule an operator authors gets the
		# same routing and the same readable title as the four this app ships.
		"assignee_resolver": resolver,
		"subject": str(extra.get("producer_task_subject_field") or "").strip(),
	}


def _task_title(recipe: dict, subject: str, source: dict) -> str:
	"""What the task is called on the board. v0.55.0 added the middle case.

	THREE SHAPES, AND THE ORDER IS A JUDGEMENT ABOUT WHO READS IT:

	  "Collect I-9 Section 2 signature for Juan Lopez"   a recipe naming `subject`
	  "Walk the cabin ... — MC-Cabin-01"                 every recipe before this
	  "Renew the certificate before it lapses"           an alert with no subject

	A DOCNAME IS THE RIGHT SUBJECT FOR A PLACE AND THE WRONG ONE FOR A PERSON.
	`MC-Cabin-01` IS what a foreman calls that cabin. `I9-2026-0043` is what
	nobody calls Juan Lopez — it is a lookup somebody has to do before the task
	means anything, and a task nobody can read at a glance is a task that sits.
	So a recipe may name ONE column on the source record to be called by instead,
	and falls back to the docname where that column is empty. It never falls back
	to nothing: a title with no subject at all would collapse fifty tasks into
	fifty identical rows on one board.
	"""
	what = str(recipe.get("what") or "")
	label = str((source or {}).get(str(recipe.get("subject") or "")) or "").strip()
	if label:
		return f"{what} for {label}"
	return f"{what} — {subject}" if subject else what


def _source_row(row: dict) -> dict:
	"""The record one alert points at, as a plain dict, or {}. Never raises.

	READ ONCE PER ALERT AND PASSED DOWN. Three things now want the source record
	— the assignee expression, the assignee resolver and the subject label on the
	task's own name — and a sweep of five hundred alerts fetching the same
	document three times is two hundred and fifty thousand reads nobody asked for.
	"""
	doctype = str(row.get("source_doctype") or "")
	docname = str(row.get("source_docname") or "")
	if not (doctype and docname):
		return {}
	try:
		return dict(frappe.get_doc(doctype, docname).as_dict())
	except Exception:
		return {}


def _assignee_for(recipe: dict, row: dict, source: dict, notes: list) -> str:
	"""Who this task goes to by name, or "" for the pool. v0.55.0.

	TWO WAYS TO NAME A PERSON, AND THE RESOLVER IS TRIED FIRST because it is the
	more specific claim. An expression reads a COLUMN ON THE ROW THAT TRIPPED —
	`row.foreman`, the person who was standing there — and a resolver walks to
	another doctype for somebody the tripped row does not mention at all. A
	recipe carrying both is a recipe whose author changed their mind; the named
	lookup wins, and the expression is still there to fall back to.
	"""
	name = str(recipe.get("assignee_resolver") or "").strip()
	if name:
		resolver = ASSIGNEE_RESOLVERS.get(name)
		if resolver is None:
			notes.append(
				f"the recipe names an assignee resolver {name!r}, which is not one this app "
				f"ships: {', '.join(sorted(ASSIGNEE_RESOLVERS))}. The task routes by skill."
			)
		elif not source:
			notes.append(
				f"{row.get('source_doctype')} {row.get('source_docname')} could not be read, so "
				f"the {name!r} routing was not resolved. The task routes by skill."
			)
		else:
			try:
				employee = str(resolver(source, notes) or "").strip()
			except Exception as exc:  # pragma: no cover - a site mid-migrate
				notes.append(f"the {name!r} routing did not run ({exc}); the task routes by skill.")
				employee = ""
			if employee and _is_employee(employee, notes):
				return employee
	return _assignee_from_expression(recipe, row, source, notes)


def _is_employee(employee: str, notes: list) -> bool:
	"""Whether a resolved name is somebody payroll has heard of."""
	if compat.doctype_exists(EMPLOYEE) and not frappe.db.exists(EMPLOYEE, employee):
		notes.append(
			f"the routing produced {employee!r}, which is not an Employee on this site. "
			"The task routes by skill rather than to a name nobody holds."
		)
		return False
	return True


def _assignee_from_expression(recipe: dict, row: dict, source: dict, notes: list) -> str:
	"""The Employee a rule's `producer_assigned_to_expression` names, or "".

	NEVER RAISES AND NEVER GUESSES. An expression that will not run, or that names
	somebody payroll has never heard of, leaves the task on its skill routing and
	says so on the report — because the alternative is a task assigned to a string
	nobody holds, which is a task that is on nobody's list AND out of the pool.
	"""
	expression = str(recipe.get("assigned_to_expression") or "").strip()
	if not expression:
		return ""
	doctype = str(row.get("source_doctype") or "")
	docname = str(row.get("source_docname") or "")
	if not (doctype and docname):
		return ""
	if not source:
		notes.append(f"{doctype} {docname} could not be read, so the assignee expression was not evaluated.")
		return ""
	try:
		from ..alerts import sandbox

		answer = sandbox.evaluate(
			expression, {"row": source, "alert": dict(row), "today": frappe.utils.today()}
		)
	except Exception as exc:
		notes.append(f"the assignee expression {expression!r} did not run ({exc}); the task routes by skill.")
		return ""
	employee = str(answer or "").strip()
	if not employee:
		notes.append(
			f"the assignee expression {expression!r} produced nothing on {doctype} {docname} — the "
			"column it reads is empty on that record — so the task routes by skill."
		)
		return ""
	if not _is_employee(employee, notes):
		return ""
	return employee


#: Alert severity to task urgency. Deliberately NOT the identity mapping: a
#: Critical alert is a statement that something has stopped being lawful, and a
#: board where every item is Critical is a board nobody reads. High is the top of
#: the working scale; Critical on a task is reserved for the handful raised
#: directly by a failed detector or a contaminated sample.
SEVERITY_URGENCY = {
	alerts.SEVERITY_CRITICAL: "High",
	alerts.SEVERITY_WARNING: "Normal",
	alerts.SEVERITY_INFO: "Low",
}


def generate_tasks_from_compliance_alerts(args: dict) -> ToolResult:
	"""Turn every open compliance alert into a dispatchable task. Idempotent.

	THE BRIDGE, AND THE POINT OF THE WHOLE RELEASE. Sprint 7 could say fifty-four
	things were wrong; nothing could send anybody to fix one. This walks the open
	alerts, maps each to the shape of work it actually is, and raises a Farm Task
	carrying the evidence its completion has to produce.

	IDEMPOTENT BY CONSTRUCTION. A task carries `source_alert`, so a second run
	finds the task the first one raised and skips the alert. Re-running after
	fixing half the camp raises tasks only for the half still outstanding, which
	is the property that makes this safe to run whenever somebody wonders.

	DRY RUN DEFAULTS FALSE, unlike this app's other bulk write, and the asymmetry
	is deliberate. `dismiss_alert_bulk` defaults to a dry run because a
	mis-typed filter there HIDES non-compliance and leaves an operation reading
	as clean while nothing was fixed. The failure mode here is the opposite and
	far cheaper: too many tasks on a board, each of which is idempotent, none of
	which changes an operational record. Gating the useful direction behind a
	second call would be safety theatre paid for by the person trying to get work
	dispatched.
	"""
	_require()
	compat.require_doctype(
		ALERT, "It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app."
	)
	company = _company(args)
	dry_run = as_bool(args, "dry_run", False)
	# max(1, ...) not `or GENERATE_CAP`: a 0 survived `min` and reached Frappe as NO LIMIT.
	limit = max(1, min(GENERATE_CAP, as_int(args, "limit", GENERATE_CAP)))

	wanted = _alert_types(args)
	filters = {"dismissed": 0}
	if company:
		filters["company"] = company
	if wanted:
		filters["alert_type"] = ("in", sorted(wanted))

	rows = frappe.db.get_all(
		ALERT,
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
		],
		order_by="due_date asc",
		limit=limit + 1,
	)
	rows = [dict(row) for row in rows or []]
	over_cap = len(rows) > limit
	rows = rows[:limit]

	answered = _already_answered([row["name"] for row in rows])
	# v0.41.0. ONE QUERY FOR THE WHOLE SWEEP — see `producer_templates`. On a site
	# where no rule names a Farm Task Template this is an empty dict and every
	# alert takes exactly the path it took before this release.
	producers = producer_templates()
	report = {
		"company": company,
		"dry_run": dry_run,
		"alerts_considered": len(rows),
		"created": [],
		"sessions": [],
		"training_sessions": [],
		"skipped_already_answered": [],
		"skipped_unmapped": [],
		"failed": [],
	}

	# v0.21.0. Before anything is raised one-alert-at-a-time, look for the places
	# where SEVERAL different things are overdue at once and one template covers
	# all of them. Those become one visit rather than N trips. Everything this
	# does not bundle falls through to the unchanged per-alert path below.
	bundled = _bundle_into_sessions(rows, answered, report, dry_run, producers)

	# v0.98.0. AND THE SAME MOVE ALONG THE OTHER AXIS. `_bundle_into_sessions`
	# groups by PLACE — several things wrong with one cabin become one visit.
	# This groups by CURRICULUM — several people whose heat-illness training is
	# lapsing become one afternoon, with all of them on the attendance sheet.
	# The two cannot collide: this one only ever looks at alerts whose source is
	# an Employee Training Record, which is not a place anybody is sent to and is
	# therefore never in `sessions.MATCHABLE_ASSET_TYPES`.
	bundled |= _bundle_into_training_sessions(rows, answered, bundled, report, dry_run)

	for row in rows:
		alert_type = str(row.get("alert_type") or "")
		if row["name"] in bundled:
			continue
		if row["name"] in answered:
			report["skipped_already_answered"].append(
				{"alert": row["name"], "alert_type": alert_type, "task": answered[row["name"]]}
			)
			continue
		recipe = _recipe_for(alert_type, producers)
		if recipe is None:
			report["skipped_unmapped"].append(
				{
					"alert": row["name"],
					"alert_type": alert_type,
					"reason": (
						f"no task recipe for {alert_type!r}. A generic task with a made-up evidence "
						"contract is worse than no task: it produces a compliance record nobody can "
						"rely on. Fill in the producer fields on the Compliance Rule with "
						"update_compliance_rule — producer_farm_task_type and evidence_contract at "
						"minimum — add it to ALERT_TASK_MAP, or raise the task by hand with "
						"create_farm_task."
					),
				}
			)
			continue
		try:
			report["created"].append(_task_from_alert(row, recipe, dry_run))
		except Exception as exc:
			report["failed"].append(
				{"alert": row["name"], "alert_type": alert_type, "error": f"{type(exc).__name__}: {exc}"}
			)

	by_type = {}
	for entry in report["created"]:
		by_type[entry["alert_type"]] = by_type.get(entry["alert_type"], 0) + 1
	report["created_count"] = len(report["created"])
	report["session_count"] = len(report["sessions"])
	report["training_session_count"] = len(report["training_sessions"])
	report["alerts_bundled_into_training_sessions"] = sum(
		len(entry["alerts"]) for entry in report["training_sessions"]
	)
	report["alerts_bundled_into_sessions"] = sum(len(entry["alerts"]) for entry in report["sessions"])
	report["by_alert_type"] = dict(sorted(by_type.items()))
	report["kanban_route"] = "/app/farm-task/view/kanban/Farm Task Dispatch"
	report["note"] = (
		("DRY RUN — nothing was written. " if dry_run else "")
		+ f"{len(report['created'])} alert(s) became dispatchable work, "
		f"{len(report['skipped_already_answered'])} already had a task, "
		f"{len(report['skipped_unmapped'])} have no recipe. Re-running is safe: a task carries the "
		"alert that produced it, so this finds what it raised last time and skips it."
	)
	if report["training_sessions"]:
		report["training_session_note"] = (
			f"{report['alerts_bundled_into_training_sessions']} lapsing training record(s) across "
			f"{len(report['training_sessions'])} curriculum(s) became ONE Training Session each "
			"instead of one Farm Task apiece. A retraining is a trainer, a room, a language and "
			"forty minutes for a crew — the delivery is one afternoon whether three people or "
			"eleven need it, and eleven cards on a board are eleven things nobody closes and no "
			"attendance sheet at the end. The session is where the badges are scanned and the "
			"signatures taken; complete_training_session is what writes the records. Only "
			"curricula ticked `group_training` are bundled, and the bundling is idempotent "
			"because the session stores the alerts it answers."
		)
	if report["sessions"]:
		report["session_note"] = (
			f"{report['alerts_bundled_into_sessions']} alert(s) at "
			f"{len(report['sessions'])} place(s) became ONE templated visit each instead of one "
			"task apiece. A worker walks into a cabin once and does everything it needs; the "
			"compliance records still come out separately, at their own cadences, because those "
			"are different regulators asking on different schedules. The bundling is deterministic "
			"— a template matched when its sections produce a superset of the records the pending "
			"alerts asked for — and it is idempotent, because the session records which alerts it "
			"answers."
		)
	if over_cap:
		report["capped"] = (
			f"More than {limit} open alerts matched. The first {limit} were considered; run again "
			"to take the next batch. An operation with this many open alerts should look at whether "
			"a rule is firing on a field that is empty everywhere rather than stale on a few."
		)
	return ToolResult(
		data=report,
		summary=(
			("dry run: would raise " if dry_run else "raised ")
			+ f"{len(report['created'])} farm task(s) from {len(rows)} open alert(s); "
			f"{len(report['skipped_already_answered'])} already answered"
		),
		docstatus_delta="" if dry_run or not report["created"] else "none → 0 (created)",
	)


def materialize_task_for_alert(args: dict) -> ToolResult:
	"""Turn ONE named alert into its dispatchable task. Idempotent, single-row twin of
	`generate_tasks_from_compliance_alerts`.

	A TAP ON THE PHONE NAMES ONE ALERT, not a filter that might also catch a coworker's.
	`generate_tasks_from_compliance_alerts` sweeps every open alert matching a company
	and a set of types — right for a nightly run, wrong for "fix the thing I am looking
	at" — so this exists to answer exactly one docname and nothing beside it, using the
	same recipe lookup and the same task-shaping code so the two paths cannot drift.

	Refuses with the same "no recipe" explanation `generate_tasks_from_compliance_alerts`
	reports in `skipped_unmapped`, rather than silently doing nothing, because a mobile
	caller has no report to read afterwards — the refusal IS the report.

	v0.106.0 TAKES TWO OPTIONAL OVERRIDES, `urgency` AND `assigned_to`, AND THE
	SWEEP TAKES NEITHER. A nightly run has no opinion about who should hold a job
	or how urgent it is beyond what the alert says; a foreman standing in front of
	one alert has both, and this is the door they come through. Neither is
	required and neither changes the recipe: the task type, the evidence contract
	and what record it has to produce are still decided by the rule, because those
	are what the alert is FOR. What the caller may decide is who and how soon.

	`assigned_to` IS SCOPE-CHECKED BY THE WRAPPER, NOT HERE, for the same reason
	every other Employee argument on the mobile surface is — `api/mobile.py`
	holds the caller's entity list and this module does not. A tool caller on the
	MCP transport is an operator's console and is trusted with a docname.
	"""
	_require()
	compat.require_doctype(
		ALERT, "It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app."
	)
	name = as_str(args, "alert", required=True)
	row = frappe.db.get_value(
		ALERT,
		name,
		[
			"name",
			"alert_type",
			"severity",
			"category",
			"company",
			"source_doctype",
			"source_docname",
			"alert_message",
			"due_date",
			"dismissed",
		],
		as_dict=True,
	)
	if not row:
		raise ToolError(f"no Compliance Alert called {name!r}. get_compliance_calendar lists them.")
	row = dict(row)
	# `compat.checked` AND NOT `bool()`. A Check field does not always come back
	# as an integer, and `bool("0")` is True — which here refused to raise a task
	# for EVERY open alert on the calendar while reporting it as dismissed, which
	# is the one refusal a caller would believe. See `compat.checked`.
	if compat.checked(row.get("dismissed")):
		raise ToolError(f"{name} is dismissed. A dismissed alert is not open work. Nothing was created.")

	existing = _already_answered([name])
	if name in existing:
		return ToolResult(
			data={
				"alert": name,
				"alert_type": row["alert_type"],
				"already_answered": True,
				"task": existing[name],
			},
			summary=f"{name} already has a task: {existing[name]}",
		)

	recipe = _recipe_for(row["alert_type"])
	if recipe is None:
		raise ToolError(
			f"no task recipe for {row['alert_type']!r}. This clears when the record behind it "
			"changes; there is no step this app can turn into a task. Fill in the producer fields "
			"on the Compliance Rule with update_compliance_rule, add it to ALERT_TASK_MAP, or raise "
			"the task by hand with create_farm_task."
		)

	entry = _task_from_alert(row, recipe, dry_run=False, overrides=_overrides(args, row))
	# v0.98.0. A GROUP CURRICULUM GETS ITS TASK AND IS TOLD ABOUT THE COHORT.
	# This tool answers ONE docname by design — a tap on a phone names the alert
	# somebody is looking at, not a filter — so it does not bundle, and bundling
	# here would raise a session for a crew the caller never asked about. What it
	# owes them instead is the sentence: the sweep will make one afternoon of
	# this, and closing eleven tasks by hand is the work that sentence saves.
	cohort = _cohort_note(row)
	if cohort:
		entry["cohort_note"] = cohort
	return ToolResult(
		data=entry,
		summary=f"{name}: raised {entry.get('task_type')} task {entry.get('task')}",
		docstatus_delta="none → 0 (created)",
	)


def _overrides(args: dict, row: dict) -> dict:
	"""The two things a single-row caller may decide, validated. v0.106.0.

	Both are checked HERE rather than left to the doctype, because a Select that
	refuses on save does it from inside `doc.insert()` — after the alert has been
	read, the recipe resolved and the routing worked out — and the refusal a
	caller gets back names a Frappe field rather than the argument they sent.
	"""
	out: dict = {}
	urgency = as_str(args, "urgency")
	if urgency:
		out["urgency"] = as_choice(FARM_TASK, "urgency", urgency, "urgency")
	assigned = as_str(args, "assigned_to")
	if assigned:
		if compat.doctype_exists(EMPLOYEE) and not frappe.db.exists(EMPLOYEE, assigned):
			raise ToolError(
				f"no Employee called {assigned!r}. Nothing was created — a task raised onto a "
				"name nobody holds is a job in a pool no one is allowed to claim."
			)
		out["assigned_to"] = assigned
	return out


def _cohort_note(row: dict) -> str:
	"""How many others are lapsing on the same curriculum, said in one sentence."""
	if str(row.get("alert_type") or "") != TRAINING_ALERT:
		return ""
	source = _training_record(row)
	curriculum = str(source.get("training_type") or "")
	if not curriculum or not compat.checked(training_sessions.type_row(curriculum).get("group_training")):
		return ""
	others = frappe.db.count(
		ALERT, {"alert_type": TRAINING_ALERT, "dismissed": 0, "company": row.get("company") or ""}
	)
	return (
		f"{curriculum} is delivered as a GROUP session, and {others} open training alert(s) name "
		f"this company. This tool raised the single task you asked for; "
		"generate_tasks_from_compliance_alerts turns the whole set into ONE Training Session "
		"with everybody on the attendance sheet, which is what the delivery actually is — a "
		"trainer, a room, a language and forty minutes, whether three people need it or eleven."
	)


def _bundle_into_sessions(
	rows: list, answered: dict, report: dict, dry_run: bool, producers: dict | None = None
) -> set:
	"""One templated visit per place where a template covers everything overdue.

	THE RULE, IN FULL, BECAUSE AN AUDITOR WILL ASK WHY THREE THINGS BECAME ONE JOB:

	  1. Alerts are grouped by the PLACE they point at — `(source_doctype,
	     source_docname)` — and only where the source doctype is a register
	     somebody can be sent to. An alert about a certificate is not about a
	     place and is never bundled.
	  2. A place qualifies only with **two or more alerts of DIFFERENT types**.
	     One alert is one task; that path is unchanged and is the common case.
	  3. Each alert type is translated into the compliance record answering it
	     would produce, through `ALERT_TASK_MAP` — the same table the per-alert
	     path uses, so the two cannot disagree about what a habitability alert
	     asks for. An alert type whose recipe produces NO record (a licence
	     renewal, a policy review) cannot be covered by a section and takes the
	     place out of the running: bundling it would produce a visit that silently
	     answers three of four overdue things.
	  4. `sessions.match_template` picks the template whose sections produce a
	     SUPERSET of those records — tightest fit first, ties broken by docname so
	     the choice is the same on every run and every site.
	  5. No match is a first-class answer and leaves every alert at that place on
	     the ordinary per-alert path.

	IDEMPOTENT, and by a different mechanism from the per-alert path. A task
	carries one `source_alert`; a session answers several, so it stores them all
	and `sessions.alerts_answered_by_open_sessions` reads them back — whole
	docnames split on newlines, never a substring match. A second sweep finds the
	session and raises nothing. Once the session is SUBMITTED its records have
	moved the registers, the alerts dismiss themselves on the next sweep, and none
	of this is reached at all.

	NEVER RAISES PAST ONE PLACE. A template that could not be matched or a session
	that could not be created lands in `failed` for that location and the alerts
	fall through to the per-alert path, which is strictly better than losing them.
	"""
	if not compat.doctype_exists(sessions.SESSION_DOCTYPE):
		return set()

	places = {}
	for row in rows:
		doctype = str(row.get("source_doctype") or "")
		docname = str(row.get("source_docname") or "")
		if not (docname and doctype in sessions.MATCHABLE_ASSET_TYPES):
			continue
		places.setdefault((doctype, docname), []).append(row)

	already = sessions.alerts_answered_by_open_sessions()
	bundled = set()
	for (doctype, docname), alerts_here in sorted(places.items()):
		if len({str(row.get("alert_type") or "") for row in alerts_here}) < 2:
			continue
		if any(row["name"] in already or row["name"] in answered for row in alerts_here):
			# Something at this place is already answered — by a session from a
			# previous sweep, or by a plain task somebody raised. Bundling the
			# rest would produce a second job overlapping the first.
			for row in alerts_here:
				entry = already.get(row["name"])
				if entry:
					bundled.add(row["name"])
					report["skipped_already_answered"].append(
						{
							"alert": row["name"],
							"alert_type": row.get("alert_type"),
							"task": entry.get("task"),
							"session": entry.get("session"),
						}
					)
			continue

		wanted = set()
		coverable = True
		for row in alerts_here:
			recipe = _recipe_for(str(row.get("alert_type") or ""), producers)
			produced = str((recipe or {}).get("creates_record") or "").strip()
			if not produced:
				coverable = False
				break
			wanted.add(produced)
		if not (coverable and wanted):
			continue

		match = sessions.match_template(doctype, wanted)
		if not match:
			continue

		entry = {
			"location": docname,
			"location_doctype": doctype,
			"template": match["template"],
			"template_name": match["template_name"],
			"template_version": match["version"],
			"covers": match["covers"],
			"extra_sections": match["extra_sections"],
			"alerts": [row["name"] for row in alerts_here],
			"alert_types": sorted({str(row.get("alert_type") or "") for row in alerts_here}),
			"session": None,
			"task": None,
		}
		if not dry_run:
			try:
				entry.update(_session_from_alerts(match, doctype, docname, alerts_here))
			except Exception as exc:
				report["failed"].append(
					{
						"location": docname,
						"template": match["template"],
						"error": f"{type(exc).__name__}: {exc}",
					}
				)
				continue
		report["sessions"].append(entry)
		bundled.update(row["name"] for row in alerts_here)
	return bundled


def _bundle_into_training_sessions(
	rows: list, answered: dict, bundled: set, report: dict, dry_run: bool
) -> set:
	"""One Training Session per curriculum, for the people whose training is lapsing.

	v0.98.0. THE SERVER USED TO FIRE N FARM TASKS FOR ONE AFTERNOON'S WORK, and
	that is the whole of the defect. `training_expiring` raises one alert per
	Employee Training Record, so a crew of eleven whose heat-illness training all
	lapses in the same fortnight produced eleven Compliance-Audit tasks, each
	saying "arrange and deliver the retraining" — eleven cards for one delivery,
	no cohort anywhere in the record, and an attendance sheet that has to be
	assembled by hand afterwards from eleven separately closed tasks. iOS has had
	`TrainingSessionRunner` and `AttendanceSignatureView` since before this
	release and nothing was ever raised for them to run.

	THE RULE, IN FULL:

	  1. Only `training_expiring` alerts, and only those whose source record
	     names a Training Type. An alert about a certificate is not about a
	     curriculum.
	  2. Only where that Training Type is ticked `group_training`. An applicator
	     licence and a forklift certification are one-to-one items and stay on
	     the per-alert path, which is unchanged.
	  3. Grouped by `(company, training_type)`. Two entities retraining the same
	     curriculum are two sessions, because a Training Session belongs to one
	     company and its records land on one payroll's people.
	  4. TWO OR MORE PEOPLE. One person needing a retraining is one task; a
	     session opened for a single attendee would be a heavier document
	     answering a lighter question, and the per-alert path already handles it
	     well.
	  5. Anything already answered — by an open session from a previous sweep or
	     by a plain task somebody raised — takes the whole curriculum out of the
	     running, exactly as `_bundle_into_sessions` does, because a second
	     delivery overlapping the first is worse than a task too many.

	NOTHING IS FILED ON ANYBODY. The session is created SCHEDULED with its
	attendees listed and `attended` unticked. `complete_training_session` is the
	only call that writes a training record, which is what makes it safe to raise
	one of these off a sweep at two in the morning.

	NEVER RAISES PAST ONE CURRICULUM. A session that could not be created lands in
	`failed` for that curriculum and its alerts fall through to the per-alert
	path, which is strictly better than losing them.
	"""
	if not training_sessions.available() or not compat.has_field(training_sessions.DOCTYPE, "source_alerts"):
		return set()

	groups = {}
	for row in rows:
		if row["name"] in bundled or str(row.get("alert_type") or "") != TRAINING_ALERT:
			continue
		source = _training_record(row)
		curriculum = str(source.get("training_type") or "")
		employee = str(source.get("employee") or "")
		if not (curriculum and employee):
			continue
		groups.setdefault((str(row.get("company") or ""), curriculum), []).append(
			{"alert": row, "employee": employee, "employee_name": source.get("employee_name") or employee}
		)

	already = training_sessions.alerts_answered_by_open_sessions()
	taken = set()
	for (company, curriculum), members in sorted(groups.items()):
		if len({entry["employee"] for entry in members}) < 2:
			continue
		curriculum_row = training_sessions.type_row(curriculum)
		if not compat.checked(curriculum_row.get("group_training")):
			continue
		if any(entry["alert"]["name"] in already or entry["alert"]["name"] in answered for entry in members):
			for entry in members:
				found = already.get(entry["alert"]["name"])
				if found:
					taken.add(entry["alert"]["name"])
					report["skipped_already_answered"].append(
						{
							"alert": entry["alert"]["name"],
							"alert_type": entry["alert"].get("alert_type"),
							"training_session": found.get("session"),
						}
					)
			continue

		record = {
			"company": company,
			"training_type": curriculum,
			"attendees": [
				{"employee": entry["employee"], "employee_name": entry["employee_name"]} for entry in members
			],
			"alerts": [entry["alert"]["name"] for entry in members],
			"training_session": None,
		}
		if not dry_run:
			try:
				record["training_session"] = _training_session_from_alerts(company, curriculum, members)
			except Exception as exc:
				report["failed"].append(
					{
						"training_type": curriculum,
						"company": company,
						"error": f"{type(exc).__name__}: {exc}",
					}
				)
				continue
		report["training_sessions"].append(record)
		taken.update(record["alerts"])
	return taken


def _training_record(row: dict) -> dict:
	"""The Employee Training Record one `training_expiring` alert points at."""
	doctype = str(row.get("source_doctype") or "")
	docname = str(row.get("source_docname") or "")
	if doctype != TRAINING_RECORD or not docname or not compat.doctype_exists(TRAINING_RECORD):
		return {}
	return dict(
		frappe.db.get_value(
			TRAINING_RECORD, docname, ["employee", "employee_name", "training_type", "company"], as_dict=True
		)
		or {}
	)


def _training_session_from_alerts(company: str, curriculum: str, members: list) -> str:
	"""Open ONE Scheduled session for a cohort, with everybody on the sheet.

	Written through `frappe.new_doc` rather than through `create_training_session`
	deliberately: that tool takes `require_shift_role` off the CALLER, and this
	runs inside a nightly sweep with no caller. The document it produces is the
	same one, and `add_session_attendee` is what a foreman uses at the door to
	turn a listed attendee into a scanned one.

	`attended` IS NOT TICKED. Listing somebody who is going to be retrained is a
	roster; ticking it would be a claim that they turned up, made before the
	afternoon happened.
	"""
	doc = frappe.new_doc(training_sessions.DOCTYPE)
	doc.training_type = curriculum
	doc.company = company or None
	doc.status = training_sessions.STATUS_SCHEDULED
	doc.session_date = frappe.utils.today()
	doc.source_alerts = "\n".join(entry["alert"]["name"] for entry in members)
	doc.notes = (
		f"Raised from {len(members)} open training alert(s) by the compliance sweep. Everybody "
		"listed has a lapsing or lapsed record for this curriculum. Scan badges at the door with "
		"add_session_attendee, take signatures with sign_session_attendance, and "
		"complete_training_session writes the records — nothing is on anybody's file until then."
	)
	for entry in members:
		doc.append(
			"attendees",
			{"employee": entry["employee"], "employee_name": entry["employee_name"], "attended": 0},
		)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _session_from_alerts(match: dict, doctype: str, docname: str, alerts_here: list) -> dict:
	"""One Farm Task carrying one Inspection Session, for a place with several alerts.

	THE FARM TASK IS STILL THE DISPATCH ATOM. It is the card on the Kanban board,
	the thing a worker claims, the thing that counts against the concurrent-claim
	limit. The session is the sectioned form behind that one card and never a
	second kind of card beside it — a board with two kinds of item on it is a
	board somebody has to be taught to read.

	`source_alert` carries the FIRST alert only, because the column holds one and
	the per-alert idempotency check reads it. The session carries all of them, and
	that is what `_bundle_into_sessions` checks on the next sweep.
	"""
	severity_order = {alerts.SEVERITY_CRITICAL: 0, alerts.SEVERITY_WARNING: 1, alerts.SEVERITY_INFO: 2}
	lead = sorted(
		alerts_here,
		key=lambda row: (severity_order.get(str(row.get("severity") or ""), 3), str(row["name"])),
	)[0]
	company = next((row.get("company") for row in alerts_here if row.get("company")), None)

	session = frappe.new_doc(sessions.SESSION_DOCTYPE)
	session.template = match["template"]
	session.location = docname
	session.location_doctype = doctype
	session.company = company
	session.state = sessions.STATE_DRAFT
	session.source_alerts = "\n".join(sorted(row["name"] for row in alerts_here))
	session.notes = "\n".join(
		f"{row.get('alert_type')}: {row.get('alert_message') or ''}".strip() for row in alerts_here
	)[:1000]
	session.insert(ignore_permissions=True)

	task = frappe.new_doc(FARM_TASK)
	task.task_name = f"{match['template_name']} — {docname}"[:140]
	task.task_type = "Inspection"
	task.state = AVAILABLE
	task.urgency = SEVERITY_URGENCY.get(str(lead.get("severity") or ""), "Normal")
	task.dispatch_mode = "Self-pick"
	task.company = company
	task.location_doctype = doctype
	task.location = docname
	task.skill_required = match["skill_required"] or None
	task.estimated_duration_minutes = match["estimated_duration_minutes"] or 0
	task.source_alert = lead["name"]
	task.inspection_session = session.name
	# The contract on the CARD is the floor; the real one is per section on the
	# template, checked by submit_inspection_session. Stating something here
	# rather than nothing keeps the task doctype's promise — there is no path to
	# a task somebody can close by saying they did it.
	task.evidence_required = json.dumps({"photos": True, "findings_text": True})
	task.notes = (
		f"One visit covering {len(alerts_here)} overdue item(s) at {docname}: "
		+ ", ".join(sorted({str(row.get("alert_type") or "") for row in alerts_here}))
		+ f". Worked from the template {match['template_name']} v{match['version']}, which has "
		f"{match['section_count']} section(s) and produces {', '.join(match['produces'])}. "
		"Submit it with submit_inspection_session — the compliance records come out separately, "
		"at their own cadences, from the one walk."
	)
	task.insert(ignore_permissions=True)
	frappe.db.set_value(sessions.SESSION_DOCTYPE, session.name, "farm_task", task.name)
	return {"session": session.name, "task": task.name, "lead_alert": lead["name"]}


def _alert_types(args: dict) -> set:
	raw = args.get("alert_types")
	if raw in (None, "", []):
		return set()
	if isinstance(raw, str):
		raw = [part.strip() for part in raw.split(",") if part.strip()]
	if not isinstance(raw, list):
		raise ToolError("alert_types must be a list of rule names, or a comma-separated string.")
	wanted = {str(entry).strip() for entry in raw if str(entry).strip()}
	unknown = sorted(wanted - set(alerts.names()))
	if unknown:
		raise ToolError(
			f"alert_types names {', '.join(repr(name) for name in unknown)}, which are not rules on "
			f"this site. list_compliance_rules has them. The mapped ones are: "
			f"{', '.join(sorted(ALERT_TASK_MAP))}."
		)
	return wanted


def _already_answered(alert_names: list) -> dict:
	"""alert docname → the Farm Task that already answers it."""
	if not alert_names:
		return {}
	rows = frappe.db.get_all(
		FARM_TASK,
		filters={"source_alert": ("in", sorted(alert_names))},
		fields=["name", "source_alert"],
		limit=GENERATE_CAP * 2,
	)
	return {str(row["source_alert"]): str(row["name"]) for row in rows or []}


def _task_from_alert(row: dict, recipe: dict, dry_run: bool, overrides: dict | None = None) -> dict:
	"""Build (and, unless this is a dry run, insert) one alert's task.

	`overrides` IS ONLY EVER THE SINGLE-ROW PATH'S. The sweep passes nothing and
	gets what it has always got: the urgency the alert's own severity implies and
	the holder the recipe's routing worked out. `materialize_task_for_alert` — a
	foreman looking at one alert and choosing — may pass `urgency` and
	`assigned_to`, and each is a DELIBERATE OVERRIDE of a server-side derivation
	rather than a value the server had no opinion about. Both are recorded in
	`routing_notes` so the answer says which of them was the caller's.
	"""
	overrides = dict(overrides or {})
	subject = str(row.get("source_docname") or "")
	# v0.22.5. Resolved BEFORE the dry-run return, so a dry run reports the person
	# the task would land on. A dry run that could not answer "who gets this" would
	# be a dry run of the half of the decision nobody is worried about.
	routing_notes: list = []
	source = _source_row(row)
	assignee = _assignee_for(recipe, row, source, routing_notes)
	# THE CALLER'S CHOICE OF PERSON BEATS THE RECIPE'S, and it is not close. A
	# resolver walks a column to guess who ought to hold this; a foreman naming
	# somebody is looking at the orchard. The recipe's own answer is still
	# reported, because "this would have gone to Ana" is what tells an operator a
	# resolver is pointing at the wrong column.
	chosen = str(overrides.get("assigned_to") or "").strip()
	if chosen:
		if assignee and assignee != chosen:
			routing_notes.append(
				f"the recipe routed this to {assignee!r}; the caller named {chosen!r} instead."
			)
		assignee = chosen
	urgency = SEVERITY_URGENCY.get(str(row.get("severity") or ""), "Normal")
	asked = str(overrides.get("urgency") or "").strip()
	if asked and asked != urgency:
		routing_notes.append(
			f"{row.get('severity') or 'Warning'} severity implies {urgency} urgency; the caller "
			f"asked for {asked}."
		)
	entry = {
		"alert": row["name"],
		"alert_type": row["alert_type"],
		"severity": row.get("severity"),
		"task_name": _task_title(recipe, subject, source),
		"task_type": recipe["task_type"],
		"urgency": asked or urgency,
		"dispatch_mode": recipe["dispatch"] if assignee else _pool_dispatch(recipe),
		# EITHER/OR, ENFORCED HERE AS WELL AS AT AUTHORING TIME. The controller
		# refuses a rule carrying both, and this is the second door: a task with a
		# named holder AND a skill would show up in the pool listing beside the
		# person already holding it.
		"skill_required": "" if assignee else recipe["skill"],
		"assigned_to": assignee or None,
		"creates_record": recipe["creates_record"] or None,
		"location_doctype": str(row.get("source_doctype") or "") or None,
		"location": subject or None,
		# v0.55.0. THE RECORD THIS TASK IS ABOUT, which for most alerts is the same
		# pair as `location` above and for a signature task is the whole point:
		# `location` is only written where the source is somewhere a worker can be
		# SENT (see `_DISPATCHABLE_LOCATIONS`), and an I-9 is not a place.
		"subject_doctype": str(row.get("source_doctype") or "") or None,
		"subject_docname": subject or None,
		"evidence_required": dict(recipe["evidence"]),
		"task": None,
	}
	# v0.41.0. A recipe that came off a Farm Task Template says so, in the dry run
	# as well as in the write — "which template decided this task's shape" is the
	# first question anybody asks of a board that changed shape overnight.
	if recipe.get("template"):
		entry["template"] = recipe["template"]
		entry["checklist"] = [
			dict(item) for item in (recipe.get("checklist_status") or {}).get("items") or []
		]
	if routing_notes:
		entry["routing_notes"] = routing_notes
	if dry_run:
		return entry

	doc = frappe.new_doc(FARM_TASK)
	doc.task_name = entry["task_name"][:140]
	doc.task_type = recipe["task_type"]
	doc.state = AVAILABLE
	doc.urgency = entry["urgency"]
	doc.dispatch_mode = entry["dispatch_mode"]
	doc.company = row.get("company") or None
	doc.skill_required = entry["skill_required"]
	doc.estimated_duration_minutes = int(recipe.get("minutes") or 0)
	doc.source_alert = row["name"]
	doc.evidence_required = json.dumps(recipe["evidence"])
	# v0.41.0. WHERE A TEMPLATE DECIDED THE SHAPE, its standing instructions come
	# FIRST and the alert's own message follows, because that is the order a
	# worker needs them in: how this job is done, then what is wrong with this
	# particular cabin. A recipe with no template leaves the notes exactly what
	# they were before this release — the alert message and nothing else.
	doc.notes = "\n\n".join(
		part
		for part in (str(recipe.get("instructions") or ""), str(row.get("alert_message") or ""))
		if part.strip()
	).strip()
	if recipe.get("template"):
		doc.template = recipe["template"]
		doc.checklist_status = json.dumps(recipe.get("checklist_status") or {"items": []})
	if assignee:
		doc.assigned_to = assignee
		doc.assigned_to_name = _worker_name(assignee, "")
		# CLAIMED rather than Available, exactly as `create_farm_task` does when
		# somebody is sent by name: the task is already held, so nobody else can
		# pick it up and it appears on that person's own list rather than in a pool
		# they would have to go looking in.
		doc.state = CLAIMED

	# A location only where the register the alert points at is one a worker can
	# be sent to. An alert about a Certification points at a certificate, which
	# is not a place — filling `location` with it would put a certificate on a
	# map and make the pool's location filter useless.
	if subject and str(row.get("source_doctype") or "") in _DISPATCHABLE_LOCATIONS:
		doc.location_doctype = row["source_doctype"]
		doc.location = subject
	# v0.55.0. The record itself, whatever kind of record it is, and it is written
	# for EVERY alert rather than only for the signature rules — "what is this
	# task about" is a question every board has always had and answered by making
	# somebody open the alert. Guarded on the column existing so a site that has
	# the app and has not yet migrated the doctype raises tasks as it always did.
	if subject and compat.has_field(FARM_TASK, "subject_doctype"):
		doc.subject_doctype = row["source_doctype"]
		doc.subject_docname = subject
	if recipe["creates_record"] and compat.doctype_exists(recipe["creates_record"]):
		spec = inspections.SPECS.get(recipe["creates_record"])
		doc.creates_record = recipe["creates_record"]
		# The template's defaults sit UNDER the subject this alert points at: the
		# template states what is usually true of the record, and the alert states
		# which cabin, block or zone this one is about. The alert wins, because it
		# is the more specific claim.
		defaults = dict(recipe.get("creates_record_data") or {})
		if spec and subject and spec.subject_doctype == str(row.get("source_doctype") or ""):
			doc.creates_record_data = json.dumps({**defaults, spec.subject_field: subject})
		elif defaults:
			doc.creates_record_data = json.dumps(defaults)
		if spec and not (subject and spec.subject_doctype == str(row.get("source_doctype") or "")):
			# The alert points at one kind of thing and the record is about
			# another — a block against an irrigation zone. Nothing is prefilled
			# and the worker is told what they have to name.
			doc.notes = (
				str(doc.notes or "")
				+ f"\n\nWhen completing this, name the {spec.subject_doctype} the sample came from "
				f'in record_data, e.g. {{"{spec.subject_field}": "<{spec.subject_doctype} docname>"}}. '
				f"One {row.get('source_doctype')} can have several, so this app will not guess."
			).strip()
	doc.insert(ignore_permissions=True)
	if assignee:
		entry["assignment"] = _open_assignment(doc, assignee, doc.assigned_to_name, dispatched=True)
	entry["task"] = doc.name
	entry["location"] = doc.location
	entry["creates_record"] = doc.creates_record or None
	return entry


def _pool_dispatch(recipe: dict) -> str:
	"""What a task falls back to when its named assignee could not be resolved.

	NEVER `Dispatched` WITH NOBODY ON IT. That combination is a task that sits in
	Available which no worker is allowed to claim — visible, urgent, and
	unreachable, which is the worst of the three ways for dispatch to fail.
	"""
	mode = str(recipe.get("dispatch") or "Either")
	# EITHER WAY OF NAMING A PERSON COUNTS. `Dispatched` with nobody on it is
	# only wrong where the recipe MEANT to name somebody and could not — a
	# recipe that never tried is a foreman's to assign, which is what Dispatched
	# has meant since v0.18.0.
	if mode == DISPATCH_DISPATCHED and (
		recipe.get("assigned_to_expression") or recipe.get("assignee_resolver")
	):
		return "Self-pick" if recipe.get("skill") else "Either"
	return mode


#: The registers an alert's source record can be a PLACE in. An alert about a
#: certificate points at a certificate, and a certificate is not somewhere
#: anybody can be sent.
_DISPATCHABLE_LOCATIONS = ("Housing Unit", "Field", "Irrigation Zone", "Parcel")


# ── 12. report_field_task ───────────────────────────────────────────────────
#: v0.23.0. Field-Initiated Tasks: a worker in the field becomes a compliance
#: sensor. The field report IS the work order — no separate "Issue" or "Ticket"
#: doctype. Photo-taking IS ticket-creation IS dispatch entry, all one act.

FIELD_REPORT_LIMIT = 5
FIELD_REPORT_WINDOW_SECONDS = 3600
FIELD_REPORT_PENALTY_HOURS = 24

#: Urgency values a field worker may choose. Critical is restricted to
#: foreman/manager roles to prevent alarm inflation — every field worker
#: believing their problem is Critical is how Critical stops meaning anything.
_WORKER_URGENCY = ("Normal", "High")

#: Roles that may use Critical urgency on a field report.
_CRITICAL_ROLES = frozenset({"Foreman", "Farm Manager"})


def _field_report_count(worker: str) -> int:
	"""Reports this worker has filed in the current window."""
	cutoff = frappe.utils.add_to_date(frappe.utils.now(), hours=-1, as_string=True, as_datetime=True)
	return frappe.db.count(
		FARM_TASK,
		filters={
			"origin": ORIGIN_FIELD_REPORTED,
			"reported_by": worker,
			"reported_at": (">=", cutoff),
		},
	)


def _is_penalty_active(worker: str) -> bool:
	"""Whether a foreman dismissed one of this worker's reports in the last 24h."""
	cutoff = frappe.utils.add_to_date(
		frappe.utils.now(), hours=-FIELD_REPORT_PENALTY_HOURS, as_string=True, as_datetime=True
	)
	return bool(
		frappe.db.exists(
			FARM_TASK,
			{
				"origin": ORIGIN_FIELD_REPORTED,
				"reported_by": worker,
				"state": CANCELLED,
				"modified": (">=", cutoff),
			},
		)
	)


def report_field_task(args: dict) -> ToolResult:
	"""A worker in the field flags a problem on the spot.

	THE FIELD REPORT IS THE WORK ORDER. No separate doctype, no two-step
	process, no gap between seeing and dispatching. The worker taps, snaps,
	describes, and the task is in the pool in one act.

	ANTI-SPAM: 5 per worker per hour. A foreman dismissing a report with
	reason 'not a real issue' counts against the reporter's limit for the
	next 24h. Photo required — a report without evidence is a rumour.

	URGENCY IS CAPPED. Workers may choose Normal or High. Critical is
	reserved for foreman/manager roles, because every worker believing their
	problem is Critical is how Critical stops meaning anything on a board.
	"""
	_require()
	company = _company(args)

	worker = _worker(args, "reported_by", required=True)
	worker_name = _worker_name(worker)

	# ── anti-spam ──────────────────────────────────────────────────────────
	if _is_penalty_active(worker):
		raise ToolError(
			f"{worker_name} ({worker}) had a field report dismissed as 'not a real issue' in the "
			f"last {FIELD_REPORT_PENALTY_HOURS} hours, which counts against their rate limit. "
			"Nothing was created."
		)
	count = _field_report_count(worker)
	if count >= FIELD_REPORT_LIMIT:
		raise ToolError(
			f"{worker_name} ({worker}) has already filed {count} field reports in the last hour. "
			f"The limit is {FIELD_REPORT_LIMIT}. Nothing was created."
		)

	# ── photo required ─────────────────────────────────────────────────────
	photo = as_str(args, "photo_file_token")
	if not photo:
		raise ToolError(
			"photo_file_token is required. A field report without a photograph is a rumour — the "
			"photo is what turns 'there is a problem' into evidence somebody can act on. Upload "
			"the photo first with stage_file_chunk / finalize_staged_file, then pass the file "
			"token here. Nothing was created."
		)
	if not frappe.db.exists("File", photo):
		raise ToolError(
			f"no File {photo!r} on this site. Upload the photo first with stage_file_chunk / "
			"finalize_staged_file, then pass the file token here. Nothing was created."
		)

	# ── urgency ────────────────────────────────────────────────────────────
	urgency = as_str(args, "urgency") or "Normal"
	if urgency == "Critical":
		caller_roles = set(frappe.get_roles(frappe.session.user or ""))
		if not (caller_roles & _CRITICAL_ROLES):
			raise ToolError(
				"Critical urgency on a field report is restricted to Foreman and Farm Manager "
				"roles. A field worker may choose Normal or High. Nothing was created."
			)
	if urgency not in ("Low", "Normal", "High", "Critical"):
		raise ToolError(
			f"urgency {urgency!r} is not one of: Low, Normal, High, Critical. Nothing was created."
		)

	# ── location ───────────────────────────────────────────────────────────
	location_doctype = as_str(args, "location_doctype")
	location = as_str(args, "location")
	if location and not location_doctype:
		raise ToolError(
			"location was given with no location_doctype. Pass the register it is in: "
			"'Housing Unit', 'Field', 'Irrigation Zone' or 'Parcel'. Nothing was created."
		)
	if location_doctype and location:
		if not compat.doctype_exists(location_doctype):
			raise ToolError(f"this site has no {location_doctype!r} DocType. Nothing was created.")
		if not frappe.db.exists(location_doctype, location):
			raise ToolError(f"no {location_doctype} called {location!r} on this site. Nothing was created.")

	# ── asset ─────────────────────────────────────────────────────────────
	# v0.98.0, item 5: `affected_asset` is the handset's spelling of `asset`.
	# One column, two words for it, because the app composes its New Task form
	# from `FarmTaskTypeGuide` and calls the field what a person calls it.
	asset_name = as_str(args, "affected_asset") or as_str(args, "asset")
	asset_doc = None
	if asset_name:
		from .asset_tags import ASSET_TYPE_SKILL_MAP, asset_row

		asset_doc = asset_row(asset_name)
		if not location and not location_doctype:
			location_doctype = asset_doc.get("location_doctype") or None
			location = asset_doc.get("location") or None

	# ── build the task ─────────────────────────────────────────────────────
	task_type = as_str(args, "task_type") or "Repair"
	skill_required = as_str(args, "skill_required")
	if not skill_required and asset_doc:
		asset_type = asset_doc.get("asset_type") or "General"
		skill_required = ASSET_TYPE_SKILL_MAP.get(asset_type, "general_maintenance")
	description = as_str(args, "description")
	task_name = description[:80] if description else f"Field report by {worker_name}"

	doc = frappe.new_doc(FARM_TASK)
	doc.task_name = task_name
	doc.task_type = as_choice(FARM_TASK, "task_type", task_type, "task_type")
	doc.urgency = urgency
	doc.origin = ORIGIN_FIELD_REPORTED
	doc.company = company
	doc.location_doctype = location_doctype or None
	doc.location = location or None
	doc.skill_required = skill_required
	doc.dispatch_mode = "Either"
	doc.state = AVAILABLE
	doc.notes = description
	doc.evidence_required = json.dumps({"photos": True, "findings_text": True})
	# `reported_by` AND `reported_at` ARE THE SERVER'S, NOT THE BODY'S. The
	# reporter is the authenticated worker — `_worker(args, "reported_by")` above
	# resolves them, and the wrapper does not declare the argument — and the
	# stamp is now, because it is the column `_field_report_count` counts the
	# five-per-hour limit on. A caller who could set either could file somebody
	# else's report, or a hundred of their own dated an hour ago. `observed_at`
	# is the settable one, and it is a different fact: see `_structured_report`.
	doc.reported_by = worker
	doc.reported_at = frappe.utils.now()
	doc.report_photo = photo
	# v0.98.0, item 5. THE ESTIMATE A FIELD REPORT COULD NOT CARRY. Every task
	# raised from a template arrives with a duration and an ad-hoc report arrived
	# with none, so it sorted last against templated work on a board that orders
	# by what it costs — a broken valve behind a fortnight of habitability walks.
	# Zero when nobody said, which is what the column has always held.
	doc.estimated_duration_minutes = as_int(args, "estimated_duration_minutes") or 0
	location_doctype, location = _structured_report(args, doc, location_doctype, location)
	doc.location_doctype = location_doctype or None
	doc.location = location or None
	if asset_doc and not doc.get("asset") and compat.has_field(FARM_TASK, "asset"):
		doc.asset = asset_doc["name"]
	doc.insert(ignore_permissions=True)

	described = _describe_task(dict(doc.as_dict()))
	return ToolResult(
		data={
			**described,
			"reported_by": worker,
			"reported_by_name": worker_name,
			"reported_at": str(doc.reported_at),
			"report_photo": photo,
		},
		summary=(
			f"field report {doc.name} ({doc.task_type}, {urgency}) "
			f"reported by {worker_name} at {doc.location or 'unspecified location'}"
		),
		docstatus_delta="none → 0 (created)",
	)


# ══════════════════════════════════════════════════════════════════════════════
# v0.79.0 — interruption, and the hour that survives it
#
# FIELD WORK IS INTERRUPTED. A worker sets an irrigation line at nine and is
# called to a broken valve at half past; the irrigating is not finished, it is
# not abandoned, and it is not being done. Until now this app had no way to say
# that, so a handset had three bad options: leave the task In-Progress and lie
# about who was working on what, complete it and lie about it being done, or
# reject it and throw away the morning.
#
# WHAT THE CLOCK HAS TO DO. `actual_duration_minutes` was the wall clock from
# `started_at` to `completed_at`, which across an interruption bills the valve
# repair to the irrigating. So a run is now a LIST OF SEGMENTS — start to pause,
# resume to pause, resume to completion — and the duration is their sum. The
# segments are the record; the total is derived from them and stored so a report
# can add a column up.
#
# ONE TASK IN PROGRESS PER WORKER, AND THAT IS ENFORCED BY PAUSING RATHER THAN
# BY REFUSING. Somebody standing at a broken valve does not want to be told they
# must first go and tidy up the job they walked away from — they want the valve
# fixed. So starting or claiming a second task AUTO-PAUSES the first, records
# that the server did it rather than the worker, and says so in the answer.
# Refusing would be defensible and would be routed around within a week.
# ══════════════════════════════════════════════════════════════════════════════

#: How the segment that a given event closed is labelled. Read by
#: `get_farm_task` and the mobile board, and worth telling apart: `auto_pause`
#: is the one nobody chose.
SEGMENT_PAUSE = "pause"
SEGMENT_AUTO_PAUSE = "auto_pause"
SEGMENT_COMPLETION = "completion"
SEGMENT_REJECTION = "rejection"
SEGMENT_MERGE = "merge"

#: Most segments one assignment will carry. A job paused two hundred times is a
#: handset in a pocket rather than a worker, and the cap is what stops one bad
#: client turning a child table into a performance problem for a whole board.
SEGMENT_CAP = 200


def _row_set(row, **values) -> None:
	"""Write onto a child row, whether it is a Document or a plain dict.

	A row this call just appended is a Document with `.set`; a row loaded back off
	a stored parent may be a plain mapping. Both shapes are real — the second is
	what a re-read gives — and a helper that assumed either one would work in the
	tests and fail in a bench, or the reverse.
	"""
	setter = getattr(row, "set", None)
	for key, value in values.items():
		if callable(setter):
			setter(key, value)
		else:
			row[key] = value


def _segment_rows(doc) -> list:
	return list(doc.get("time_segments") or [])


def _open_segment(doc):
	"""The segment that is still running on this assignment, or None."""
	for row in _segment_rows(doc):
		if not row.get("ended_at"):
			return row
	return None


def _start_segment(doc, when: str) -> None:
	"""Open a stretch of work. Refuses to open a second one.

	A second open segment would double-count every minute between the two, which
	is the failure mode that makes a timesheet indefensible rather than merely
	wrong.
	"""
	if _open_segment(doc) is not None:
		return
	if len(_segment_rows(doc)) >= SEGMENT_CAP:
		raise ToolError(
			f"{doc.name} already has {SEGMENT_CAP} time segments on it, which is a handset in "
			"somebody's pocket rather than a worker. Complete or reject it and raise a new task. "
			"Nothing was changed."
		)
	doc.append("time_segments", {"started_at": when, "ended_at": None, "minutes": 0.0})


def _close_segment(doc, when: str, ended_by: str, reason: str = "") -> float:
	"""Stop the running stretch and return its minutes. Zero where none was open.

	NEVER RAISES ON A MISSING SEGMENT. An assignment started before v0.79.0 has
	no segments at all, and refusing to complete it would strand every task that
	was open on the day this shipped. The fallback is the old arithmetic, which
	is what those rows have always meant.
	"""
	row = _open_segment(doc)
	if row is None:
		return 0.0
	minutes = float(_elapsed(row.get("started_at"), when))
	_row_set(row, ended_at=when, minutes=minutes, ended_by=ended_by)
	if reason:
		_row_set(row, reason=reason)
	return minutes


def active_minutes(doc) -> int:
	"""The sum of the closed segments, or the wall clock where there are none.

	THE FALLBACK IS THE POINT OF THE SECOND BRANCH. Every assignment written
	before v0.79.0 has an empty `time_segments` table and a perfectly good
	`started_at`/`completed_at` pair, and a duration that came back zero for all
	of them would rewrite a season of history the day this shipped.
	"""
	rows = _segment_rows(doc)
	if rows:
		return round(sum(float(row.get("minutes") or 0) for row in rows))
	return int(_elapsed(doc.get("started_at"), doc.get("completed_at")))


def _describe_segments(doc) -> list:
	out = []
	for row in _segment_rows(doc):
		out.append(
			{
				"started_at": str(row.get("started_at") or "") or None,
				"ended_at": str(row.get("ended_at") or "") or None,
				"minutes": round(float(row.get("minutes") or 0), 1),
				"ended_by": row.get("ended_by") or None,
				"reason": row.get("reason") or None,
				"running": not row.get("ended_at"),
			}
		)
	return out


def in_progress_assignment(worker: str, exclude_task: str = "") -> dict:
	"""The one assignment this worker is actually working right now, or `{}`.

	ONE, BY CONSTRUCTION. `EXCLUSIVE_STATES` says a worker is In-Progress on at
	most one task, and every door that could open a second one auto-pauses the
	first — so this returning a list would be describing a state the app does
	not allow. Where a site somehow has two (a Desk edit, a half-finished
	migration), the OLDEST is returned, because that is the one that has been
	wrong for longest.
	"""
	worker = str(worker or "").strip()
	if not worker:
		return {}
	rows = (
		frappe.db.get_all(
			FARM_TASK_ASSIGNMENT,
			filters={"assigned_to": worker, "state": IN_PROGRESS},
			fields=compat.existing_fields(FARM_TASK_ASSIGNMENT, _ASSIGNMENT_FIELDS),
			order_by="creation asc",
			limit=5,
		)
		or []
	)
	for row in rows:
		row = dict(row)
		if exclude_task and str(row.get("task")) == exclude_task:
			continue
		return row
	return {}


def _pause_assignment(name: str, reason: str, when: str, automatic: bool) -> dict:
	"""Move one assignment to Paused and stop its clock. The shared write.

	`pause_farm_task` and the auto-pause on claim/start both go through here, so
	a job a worker paused and a job the server paused leave the same shape of
	evidence — same state, same closed segment, same count — differing only in
	`auto_paused` and the segment's `ended_by`. A near-copy for the automatic
	path is how the two would come to disagree about what a pause is.
	"""
	doc = frappe.get_doc(FARM_TASK_ASSIGNMENT, name)
	minutes = _close_segment(doc, when, SEGMENT_AUTO_PAUSE if automatic else SEGMENT_PAUSE, reason)
	doc.state = PAUSED
	doc.paused_at = when
	doc.pause_reason = reason or None
	doc.pause_count = int(doc.pause_count or 0) + 1
	doc.auto_paused = 1 if automatic else 0
	doc.actual_duration_minutes = active_minutes(doc)
	doc.save(ignore_permissions=True)
	_set_task_state(doc.task, PAUSED)
	return {
		"assignment": doc.name,
		"task": doc.task,
		"task_name": doc.task_name,
		"paused_at": str(doc.paused_at or ""),
		"reason": reason or None,
		"segment_minutes": round(minutes, 1),
		"active_minutes": int(doc.actual_duration_minutes or 0),
		"pause_count": int(doc.pause_count or 0),
		"auto_paused": bool(automatic),
	}


def _auto_pause_for(worker: str, exclude_task: str, reason: str) -> dict | None:
	"""Stand down whatever this worker had running, so the new job can start.

	RETURNS THE RECORD RATHER THAN SWALLOWING IT. The handset has to be able to
	say "your irrigation job was paused" — a server that silently stood a job
	down would leave a worker discovering at the end of the day that their
	morning is attributed to a task they thought they were still on.
	"""
	running = in_progress_assignment(worker, exclude_task=exclude_task)
	if not running:
		return None
	return _pause_assignment(str(running["name"]), reason, frappe.utils.now(), automatic=True)


# ── pause_farm_task ─────────────────────────────────────────────────────────
def pause_farm_task(args: dict) -> ToolResult:
	"""Stop the clock on a task a worker is coming back to."""
	_require()
	assignment = _assignment_for(args)
	worker = _worker(args, required=False)
	if worker and assignment.get("assigned_to") != worker:
		raise ToolError(
			f"{assignment['task']} is held by {assignment.get('assigned_to_name')}, not {worker}. "
			"Nothing was changed."
		)
	if assignment.get("state") == PAUSED:
		raise ToolError(
			f"{assignment['name']} was already paused at {assignment.get('paused_at')}. Pausing it "
			"twice would close a segment that is not open and leave the count saying a worker "
			"stopped twice for one interruption. Nothing was changed."
		)
	if assignment.get("state") != IN_PROGRESS:
		raise ToolError(
			f"{assignment['name']} is {assignment.get('state')}, not {IN_PROGRESS}. Only work that "
			"is actually being done can be interrupted — a claimed task nobody has started has no "
			"clock to stop. Nothing was changed."
		)

	reason = as_str(args, "reason")
	when = as_str(args, "paused_at") or frappe.utils.now()
	paused = _pause_assignment(assignment["name"], reason, when, automatic=False)

	return ToolResult(
		data={
			**paused,
			"state": PAUSED,
			"task_detail": _describe_task(task_row(assignment["task"])),
			"note": (
				"The clock is stopped and the task is still yours. `resume_farm_task` picks it back "
				"up; the minutes already on it are kept, and the gap is not charged to this job."
			),
		},
		summary=(
			f"{assignment.get('assigned_to_name')} paused {assignment['task']} after "
			f"{paused['segment_minutes']} min" + (f": {reason}" if reason else "")
		),
		docstatus_delta=f"{IN_PROGRESS} → {PAUSED}",
	)


# ── resume_farm_task ────────────────────────────────────────────────────────
def resume_farm_task(args: dict) -> ToolResult:
	"""Pick a paused task back up. Opens a new segment; the old minutes stay."""
	_require()
	assignment = _assignment_for(args)
	worker = _worker(args, required=False)
	if worker and assignment.get("assigned_to") != worker:
		raise ToolError(
			f"{assignment['task']} is held by {assignment.get('assigned_to_name')}, not {worker}. "
			"Nothing was changed."
		)
	if assignment.get("state") == IN_PROGRESS:
		raise ToolError(f"{assignment['name']} is already in progress. Nothing was changed.")
	if assignment.get("state") != PAUSED:
		raise ToolError(
			f"{assignment['name']} is {assignment.get('state')}, not {PAUSED}. Nothing was changed."
		)

	holder = str(assignment.get("assigned_to") or "")
	# RESUMING IS STARTING, so the same exclusivity applies: whatever this worker
	# had running is stood down first. Without this a resume would be the one door
	# left that could put somebody In-Progress on two jobs at once.
	auto_paused = _auto_pause_for(
		holder,
		exclude_task=str(assignment["task"]),
		reason=f"Resumed {assignment['task']}",
	)

	when = as_str(args, "resumed_at") or frappe.utils.now()
	doc = frappe.get_doc(FARM_TASK_ASSIGNMENT, assignment["name"])
	_start_segment(doc, when)
	doc.state = IN_PROGRESS
	doc.paused_at = None
	doc.auto_paused = 0
	doc.actual_duration_minutes = active_minutes(doc)
	doc.save(ignore_permissions=True)
	_set_task_state(assignment["task"], IN_PROGRESS)

	data = {
		"assignment": doc.name,
		"task": doc.task,
		"task_name": doc.task_name,
		"state": IN_PROGRESS,
		"resumed_at": when,
		"active_minutes": int(doc.actual_duration_minutes or 0),
		"pause_count": int(doc.pause_count or 0),
		"segments": _describe_segments(doc),
		"task_detail": _describe_task(task_row(assignment["task"])),
		"note": (
			f"{int(doc.actual_duration_minutes or 0)} minute(s) were already on this job before the "
			"interruption and are kept. The clock is running again from now."
		),
	}
	if auto_paused:
		data["auto_paused"] = auto_paused
		data["auto_pause_note"] = (
			f"{auto_paused['task_name'] or auto_paused['task']} was in progress and has been paused "
			"so this one can run. Nobody is in two places at once."
		)

	return ToolResult(
		data=data,
		summary=f"{doc.assigned_to_name} resumed {assignment['task']} ({data['active_minutes']} min so far)",
		docstatus_delta=f"{PAUSED} → {IN_PROGRESS}",
	)


# ══════════════════════════════════════════════════════════════════════════════
# v0.79.0 — two people, one broken valve
#
# TWO TASKS FOR ONE JOB IS NOT A BUG, IT IS WHAT HAPPENS. Two workers walk past
# the same leaking valve an hour apart and both do the right thing: they report
# it. A dispatch board that silently deduplicated them would be guessing that
# two reports of a valve are the same valve — and on a farm with four hundred of
# them, sometimes they are not.
#
# SO THE SYSTEM SURFACES AND THE HUMAN DECIDES. `_duplicate_hint` puts the other
# open task in front of whoever claims or starts one; `link_farm_tasks` records
# that two jobs are related, on BOTH sides; `merge_farm_task` folds a duplicate
# into a primary. Nothing merges itself.
#
# A MERGE MOVES THE EVIDENCE AND KEEPS THE HISTORY. The duplicate carries
# somebody's photographs, their minutes and their account of what they found —
# throwing that away to tidy a board would be destroying the record to improve
# the view of it. So the evidence transfers to the primary, the merged task's
# assignments and time segments stay exactly where they are (their minutes show
# up in the combined effort), and the task itself goes to `Merged` with
# `merged_into` pointing at where the work went.
# ══════════════════════════════════════════════════════════════════════════════

#: Relationships a link may carry. `related` is two people working one job on
#: purpose; `duplicate_of` and `merged_from` are what a merge writes on each
#: side. The rest are ordinary dispatch vocabulary.
LINK_RELATED = "related"
LINK_DUPLICATE_OF = "duplicate_of"
LINK_MERGED_FROM = "merged_from"
LINK_RELATIONSHIPS = (LINK_RELATED, LINK_DUPLICATE_OF, LINK_MERGED_FROM, "blocked_by", "follows")

#: The mirror of each relationship, for the row written on the other side. A
#: link is one fact about two records and both of them have to be able to state
#: it — `A duplicate_of B` means `B merged_from A` when it is acted on, and a
#: table that stored only the forward direction would make the relationship
#: invisible from whichever record somebody happened to open.
_MIRROR = {
	LINK_RELATED: LINK_RELATED,
	LINK_DUPLICATE_OF: LINK_MERGED_FROM,
	LINK_MERGED_FROM: LINK_DUPLICATE_OF,
	"blocked_by": "follows",
	"follows": "blocked_by",
}

#: Most links one task carries. Past this somebody is linking a whole board
#: together, which is a conversation rather than a data structure.
LINK_CAP = 50


def _link_rows(doc) -> list:
	return list(doc.get("linked_tasks") or [])


def _already_linked(doc, other: str) -> bool:
	return any(str(row.get("linked_task")) == other for row in _link_rows(doc))


def _write_link(doc, other: dict, relationship: str, by: str, note: str) -> bool:
	"""Add one link row. Returns whether it was new.

	IDEMPOTENT ON THE PAIR rather than on the relationship, so re-linking two
	tasks does not stack rows — and a caller correcting a relationship removes
	the link and writes it again rather than ending up with two contradictory
	rows on one record.
	"""
	if _already_linked(doc, str(other["name"])):
		return False
	if len(_link_rows(doc)) >= LINK_CAP:
		raise ToolError(
			f"{doc.name} already links to {LINK_CAP} other tasks, which is a board being tied "
			"together rather than a job. Nothing was changed."
		)
	doc.append(
		"linked_tasks",
		{
			"linked_task": other["name"],
			"linked_task_name": other.get("task_name"),
			"relationship": relationship,
			"linked_by": by or None,
			"linked_on": frappe.utils.now(),
			"note": note or None,
		},
	)
	return True


def _describe_links(row: dict) -> list:
	"""The link table off a stored Farm Task row, as plain dicts."""
	out = []
	for link in row.get("linked_tasks") or []:
		link = dict(link)
		out.append(
			{
				"task": link.get("linked_task"),
				"task_name": link.get("linked_task_name") or None,
				"relationship": link.get("relationship") or LINK_RELATED,
				"linked_by": link.get("linked_by") or None,
				"linked_on": str(link.get("linked_on") or "") or None,
				"note": link.get("note") or None,
			}
		)
	return out


def open_tasks_on(asset: str, exclude: str = "", location_doctype: str = "", location: str = "") -> list:
	"""Live tasks against the same thing, for the duplicate hint. Never raises.

	BOTH ROUTES TO A PLACE, because a field report writes `asset` and a
	dispatched job writes the dynamic `location` pair, and a hint that read one
	column would miss exactly the case it exists for: a worker reporting a valve
	somebody has already been sent to.
	"""
	if not compat.doctype_exists(FARM_TASK):
		return []
	live = [state for state in STATES if state not in TERMINAL_STATES]
	filter_sets = []
	if asset and compat.has_field(FARM_TASK, "asset"):
		filter_sets.append({"asset": asset})
	if location and location_doctype and compat.has_field(FARM_TASK, "location"):
		filter_sets.append({"location_doctype": location_doctype, "location": location})
	seen: dict = {}
	for filters in filter_sets:
		try:
			rows = (
				frappe.db.get_all(
					FARM_TASK,
					filters={**filters, "state": ("in", live)},
					fields=compat.existing_fields(
						FARM_TASK,
						(
							"name",
							"task_name",
							"task_type",
							"state",
							"assigned_to",
							"assigned_to_name",
							"creation",
						),
					),
					order_by="creation asc",
					limit=20,
				)
				or []
			)
		except Exception:  # pragma: no cover - a site shaping these columns differently
			continue
		for raw in rows:
			raw = dict(raw)
			if str(raw.get("name")) == exclude:
				continue
			seen.setdefault(str(raw.get("name")), raw)
	return list(seen.values())


def _duplicate_hint(task: dict) -> dict | None:
	""" "There is already an open task for this asset" — the sentence, not the act.

	SURFACED, NEVER ACTED ON. Two reports of a valve are sometimes two valves,
	and a server that merged them on a name match would be destroying one
	worker's record on a guess. The hint carries the other task, who has it, and
	the two calls a person can make about it; the decision is the foreman's.

	Never raises: a hint that failed would take a claim down with it, and a claim
	is the call somebody makes standing in front of the work.
	"""
	try:
		others = open_tasks_on(
			str(task.get("asset") or ""),
			exclude=str(task.get("name") or ""),
			location_doctype=str(task.get("location_doctype") or ""),
			location=str(task.get("location") or ""),
		)
	except Exception:  # pragma: no cover - a hint, never a refusal
		return None
	if not others:
		return None
	first = others[0]
	holder = first.get("assigned_to_name") or first.get("assigned_to") or "nobody yet"
	return {
		"open_task_count": len(others),
		"tasks": [
			{
				"name": row.get("name"),
				"task_name": row.get("task_name"),
				"task_type": row.get("task_type"),
				"state": row.get("state"),
				"assigned_to_name": row.get("assigned_to_name") or row.get("assigned_to") or None,
				"raised_at": str(row.get("creation") or "") or None,
			}
			for row in others
		],
		"message": (
			f"There is already an open task for this: {first.get('task_name')} "
			f"({first.get('name')}) held by {holder}. Link to it?"
		),
		"message_key": "task.duplicate_hint",
		"actions": ["link_farm_tasks", "merge_farm_task"],
		"note": (
			"NOTHING WAS MERGED. Two reports of one valve are sometimes two valves, and this app "
			"does not guess which. Link them if two people are working the job together; merge the "
			"duplicate into the primary if it is one job reported twice."
		),
	}


# ── link_farm_tasks ─────────────────────────────────────────────────────────
def link_farm_tasks(args: dict) -> ToolResult:
	"""Record that two tasks are related. Written on both sides."""
	_require()
	first = task_row(as_str(args, "task", required=True))
	second = task_row(as_str(args, "linked_task", required=True) or as_str(args, "other_task"))
	if first["name"] == second["name"]:
		raise ToolError(f"{first['name']} cannot be linked to itself. Nothing was changed.")

	relationship = as_str(args, "relationship") or LINK_RELATED
	if relationship not in LINK_RELATIONSHIPS:
		raise ToolError(
			f"relationship must be one of {', '.join(LINK_RELATIONSHIPS)}, not {relationship!r}. "
			"Nothing was changed."
		)
	by = as_str(args, "linked_by") or (frappe.session.user if hasattr(frappe, "session") else "")
	note = as_str(args, "note")

	forward = frappe.get_doc(FARM_TASK, first["name"])
	back = frappe.get_doc(FARM_TASK, second["name"])
	wrote_forward = _write_link(forward, second, relationship, by, note)
	wrote_back = _write_link(back, first, _MIRROR.get(relationship, relationship), by, note)
	if wrote_forward:
		forward.save(ignore_permissions=True)
	if wrote_back:
		back.save(ignore_permissions=True)

	if not (wrote_forward or wrote_back):
		raise ToolError(f"{first['name']} and {second['name']} are already linked. Nothing was changed.")

	return ToolResult(
		data={
			"task": first["name"],
			"linked_task": second["name"],
			"relationship": relationship,
			"reverse_relationship": _MIRROR.get(relationship, relationship),
			"linked_by": by or None,
			"note": note or None,
			"links_on_task": _describe_links(dict(forward.as_dict())),
			"links_on_linked_task": _describe_links(dict(back.as_dict())),
			"detail": (
				"The link is written on BOTH records. A relationship stored on one side only is "
				"invisible from whichever of the two somebody happens to open, and the whole point "
				"is that the second person to walk up to a job finds the first person's."
			),
		},
		summary=f"linked {first['name']} ↔ {second['name']} ({relationship})",
		docstatus_delta="0 → 0 (updated)",
	)


def _transfer_evidence(from_task: str, into_task: str) -> dict:
	"""Move a merged task's evidence onto the primary. Reports what moved.

	THE ASSIGNMENTS THEMSELVES DO NOT MOVE. A Farm Task Assignment is the record
	that a named person was sent, went and did something — re-pointing it at
	another task would rewrite whose work it was. What moves is the EVIDENCE:
	the photographs and signatures filed against the duplicate, copied onto the
	primary's own completion so the record that survives carries the proof both
	people produced.

	The originals are left in place. A copy is not a loss and a move would make
	the merged task's own history unreadable — which is the thing `Merged`
	exists to avoid.
	"""
	moved, minutes = [], 0
	assignments = (
		frappe.db.get_all(
			FARM_TASK_ASSIGNMENT,
			filters={"task": from_task},
			fields=compat.existing_fields(FARM_TASK_ASSIGNMENT, _ASSIGNMENT_FIELDS),
			order_by="creation asc",
			limit=50,
		)
		or []
	)
	target = live_assignment(into_task) or _last_completion(into_task)
	target_doc = frappe.get_doc(FARM_TASK_ASSIGNMENT, target) if target else None

	for row in assignments:
		row = dict(row)
		minutes += int(row.get("actual_duration_minutes") or 0)
		if not target_doc:
			continue
		source = frappe.get_doc(FARM_TASK_ASSIGNMENT, str(row["name"]))
		for evidence in source.get("evidence_files") or []:
			entry = dict(evidence)
			entry.pop("name", None)
			entry.pop("parent", None)
			entry.pop("parenttype", None)
			entry.pop("parentfield", None)
			entry.pop("idx", None)
			caption = str(entry.get("caption") or "").strip()
			entry["caption"] = (
				f"{caption} (merged from {from_task})" if caption else f"Merged from {from_task}"
			)
			target_doc.append("evidence_files", entry)
			moved.append(entry.get("file") or entry.get("file_url"))
	if target_doc and moved:
		target_doc.save(ignore_permissions=True)

	return {
		"evidence_moved": [name for name in moved if name],
		"evidence_moved_count": len(moved),
		"target_assignment": target or None,
		"minutes_from_merged_task": minutes,
		"assignments_preserved": [str(row["name"]) for row in assignments],
	}


# ── merge_farm_task ─────────────────────────────────────────────────────────
def merge_farm_task(args: dict) -> ToolResult:
	"""Fold a duplicate into the task that is actually being worked.

	THE PRIMARY KEEPS ITS STATE AND ITS CLOCK. A merge is a statement about which
	record the work is being done under, not an event in the work itself — so the
	primary's assignment, its segments and its minutes are untouched, and a
	worker who was mid-job does not find their clock reset because a foreman
	tidied the board.

	THE DUPLICATE IS NOT DELETED. It goes to `Merged` with `merged_into` naming
	the primary; its assignments, its time segments and its narrative stay
	exactly where they are. `combined_minutes` is what a report reads for the
	effort both people actually put in.
	"""
	_require()
	primary = task_row(as_str(args, "into", required=True) or as_str(args, "primary"))
	duplicate = task_row(as_str(args, "task", required=True) or as_str(args, "duplicate"))

	if primary["name"] == duplicate["name"]:
		raise ToolError("A task cannot be merged into itself. Nothing was changed.")
	if duplicate["state"] == MERGED:
		existing = frappe.db.get_value(FARM_TASK, duplicate["name"], "merged_into")
		raise ToolError(f"{duplicate['name']} was already merged into {existing}. Nothing was changed.")
	if duplicate["state"] in TERMINAL_STATES:
		raise ToolError(
			f"{duplicate['name']} is {duplicate['state']}. A finished task is a record of work that "
			"happened and folding it away would hide it — link it instead if the two belong "
			"together. Nothing was changed."
		)
	if primary["state"] in TERMINAL_STATES:
		raise ToolError(
			f"{primary['name']} is {primary['state']}, so it is not where any more work is going. "
			"Merge into the task somebody is actually doing. Nothing was changed."
		)

	reason = as_str(args, "reason")
	if not reason:
		raise ToolError(
			"reason is required. A merge takes one worker's job off the board under somebody "
			"else's name, and 'duplicate of the valve report from this morning' is what makes that "
			"reviewable six weeks later. Nothing was changed."
		)
	by = as_str(args, "merged_by") or (frappe.session.user if hasattr(frappe, "session") else "")

	transfer = _transfer_evidence(duplicate["name"], primary["name"])

	# The duplicate's live assignment is closed as Merged rather than left
	# holding a task that is no longer being worked — otherwise its worker's
	# concurrent-claim count would carry a job that has gone.
	live = live_assignment(duplicate["name"])
	if live:
		doc = frappe.get_doc(FARM_TASK_ASSIGNMENT, live)
		_close_segment(doc, frappe.utils.now(), SEGMENT_MERGE, f"Merged into {primary['name']}")
		doc.state = MERGED
		doc.paused_at = None
		doc.actual_duration_minutes = active_minutes(doc)
		doc.save(ignore_permissions=True)

	merged_doc = frappe.get_doc(FARM_TASK, duplicate["name"])
	primary_doc = frappe.get_doc(FARM_TASK, primary["name"])
	_write_link(merged_doc, primary, LINK_DUPLICATE_OF, by, reason)
	_write_link(primary_doc, duplicate, LINK_MERGED_FROM, by, reason)
	merged_doc.state = MERGED
	merged_doc.merged_into = primary["name"]
	merged_doc.assigned_to = ""
	merged_doc.assigned_to_name = ""
	merged_doc.save(ignore_permissions=True)
	primary_doc.save(ignore_permissions=True)

	primary_minutes = sum(
		int(row.get("actual_duration_minutes") or 0)
		for row in frappe.db.get_all(
			FARM_TASK_ASSIGNMENT,
			filters={"task": primary["name"]},
			fields=["actual_duration_minutes"],
			limit=50,
		)
		or []
	)

	return ToolResult(
		data={
			"merged": duplicate["name"],
			"into": primary["name"],
			"reason": reason,
			"merged_by": by or None,
			"primary_state": primary_doc.state,
			**transfer,
			"minutes_on_primary": primary_minutes,
			"combined_minutes": primary_minutes + int(transfer["minutes_from_merged_task"] or 0),
			"note": (
				f"{duplicate['name']} is Merged and points at {primary['name']}. Nothing was "
				"deleted: its assignments, its time segments and its narrative stay on it, so the "
				"effort both people put in is still countable and somebody opening the duplicate "
				"six weeks later is told where the work went."
			),
		},
		summary=(
			f"merged {duplicate['name']} into {primary['name']}: {transfer['evidence_moved_count']} "
			f"evidence file(s) moved, {transfer['minutes_from_merged_task']} min carried"
		),
		docstatus_delta=f"{duplicate['state']} → {MERGED}",
	)


# ══════════════════════════════════════════════════════════════════════════════
# v0.79.0 — work that does not finish today
#
# AN ACCIDENT INVESTIGATION IS NOT A TASK, IT IS A SET OF THEM. Interview the
# witness. Photograph the scene. Pull the camera footage. Write the root cause.
# Those happen on different days, by different people, and the investigation is
# not finished until they all are — which is a shape this app's dispatch board
# could not express: every task was a thing one person did in one visit.
#
# SO A TASK MAY HAVE A PARENT. Each child carries its own assignee, its own
# clock and its own state; the parent carries the narrative and does not close
# while a child is live. That last rule is what makes a multi-day investigation
# survive an evening — without it, the first person to finish their piece closes
# the investigation, and the camera footage nobody pulled becomes a finding
# nobody made.
#
# NOTHING AUTO-CLOSES AT THE END OF A SHIFT, and this is worth stating because
# the crew clock does close things: `end_shift` ends a SHIFT, and a task is not
# a shift. A worker who goes home mid-investigation leaves it In-Progress or
# Paused, and picks it up in the morning.
# ══════════════════════════════════════════════════════════════════════════════

#: Most children one parent carries. An investigation with more steps than this
#: is a project, and this app is not a project planner.
SUBTASK_CAP = 100


def subtasks_of(task: str, limit: int = SUBTASK_CAP) -> list:
	"""Every task whose parent is this record, oldest first. Never raises.

	KEYED ON THE DOCNAME ALONE and not on the doctype pair, because the caller
	holds one name and the pair would make every call site learn which register
	it was asking about. Docnames across these two registers do not collide —
	`FT-2026-08-00012` and an Accident Report's hash are not the same string —
	so the narrower filter would buy nothing and cost every caller an argument.
	"""
	if not compat.doctype_exists(FARM_TASK) or not compat.has_field(FARM_TASK, "parent_task"):
		return []
	try:
		rows = (
			frappe.db.get_all(
				FARM_TASK,
				filters={"parent_task": task},
				fields=compat.existing_fields(
					FARM_TASK,
					(
						"name",
						"task_name",
						"task_type",
						"state",
						"urgency",
						"assigned_to",
						"assigned_to_name",
						"creation",
					),
				),
				order_by="creation asc",
				limit=limit,
			)
			or []
		)
	except Exception:  # pragma: no cover - a site shaping these columns differently
		return []
	out = []
	for raw in rows:
		row = dict(raw)
		out.append(
			{
				"name": row.get("name"),
				"task_name": row.get("task_name"),
				"task_type": row.get("task_type") or None,
				"state": row.get("state"),
				"urgency": row.get("urgency") or "Normal",
				"assigned_to": row.get("assigned_to") or None,
				"assigned_to_name": row.get("assigned_to_name") or row.get("assigned_to") or None,
				"open": row.get("state") not in TERMINAL_STATES,
				"raised_at": str(row.get("creation") or "") or None,
			}
		)
	return out


def open_subtasks(task: str) -> list:
	return [child for child in subtasks_of(task) if child["open"]]


def subtask_summary(task: str) -> dict:
	"""The parent's own progress line: how many of its steps are done.

	Returned on a scan and on `get_farm_task`, because "3 of 5 done, waiting on
	the camera footage" is the sentence somebody wants and counting the list
	client-side is how two screens come to disagree.
	"""
	children = subtasks_of(task)
	if not children:
		return {"subtask_count": 0, "subtasks": [], "open_subtask_count": 0, "subtasks_complete": True}
	open_children = [child for child in children if child["open"]]
	return {
		"subtask_count": len(children),
		"open_subtask_count": len(open_children),
		"subtasks_complete": not open_children,
		"subtasks": children,
		"progress": f"{len(children) - len(open_children)} of {len(children)} done",
		"waiting_on": [child["task_name"] or child["name"] for child in open_children],
	}


#: Which registers may own steps. An Accident Report is the reason this is not
#: simply "another Farm Task": an investigation's steps are interviews and
#: photographs and a root-cause write-up, and the investigation itself is not a
#: dispatchable job. Anything else is refused rather than accepted, because
#: "hang a task off any doctype" is a general-purpose project tree, which this
#: app is deliberately not.
PARENT_DOCTYPES = (FARM_TASK, "Accident Report")


def _parent_argument(args: dict, verb: str) -> tuple[str, str]:
	"""`(parent_doctype, parent)` for a create call. `("", "")` where none given.

	REFUSES A TERMINAL PARENT and refuses a chain. A step added to a finished
	investigation is a step nobody will see; and a sub-task of a sub-task is a
	project plan, which is a different tool and one this app is not.
	"""
	parent = as_str(args, "parent_task")
	if not parent:
		return "", ""
	if not compat.has_field(FARM_TASK, "parent_task"):
		raise ToolError(
			"this site's Farm Task has no parent_task column, so a sub-task has nowhere to hang — "
			f"run `bench --site <site> migrate` after upgrading the app. Nothing was {verb}."
		)

	doctype = as_str(args, "parent_doctype")
	if not doctype:
		# INFERRED WHERE IT CAN BE, because a handset raising a step from an
		# investigation screen holds one docname and should not have to name the
		# register this app keeps it in.
		found = [
			candidate
			for candidate in PARENT_DOCTYPES
			if compat.doctype_exists(candidate) and frappe.db.exists(candidate, parent)
		]
		if len(found) > 1:
			raise ToolError(
				f"{parent!r} is a record in {' and in '.join(found)}. Pass parent_doctype. "
				f"Nothing was {verb}."
			)
		if not found:
			raise ToolError(
				f"no {' or '.join(PARENT_DOCTYPES)} called {parent!r} on this site. Nothing was {verb}."
			)
		doctype = found[0]
	if doctype not in PARENT_DOCTYPES:
		raise ToolError(
			f"parent_doctype must be one of {', '.join(PARENT_DOCTYPES)}, not {doctype!r}. Hanging "
			"a task off an arbitrary record would be a project tree, which this app is not. "
			f"Nothing was {verb}."
		)
	if not frappe.db.exists(doctype, parent):
		raise ToolError(f"no {doctype} called {parent!r} on this site. Nothing was {verb}.")

	if doctype == FARM_TASK:
		row = task_row(parent)
		if row["state"] in TERMINAL_STATES:
			raise ToolError(
				f"{row['name']} is {row['state']}, so a step added to it is a step nobody will see. "
				f"Nothing was {verb}."
			)
		grandparent = frappe.db.get_value(FARM_TASK, row["name"], "parent_task")
		if grandparent:
			raise ToolError(
				f"{row['name']} is itself a sub-task of {grandparent}. One level of nesting is "
				"deliberate: a tree of sub-tasks is a project plan, and a dispatch board that "
				f"became one would stop being readable at a tailgate. Nothing was {verb}."
			)
		parent = row["name"]
	elif str(frappe.db.get_value(doctype, parent, "status") or "") == "Closed":
		raise ToolError(
			f"{doctype} {parent} is closed, so a step added to it is a step nobody will see. "
			f"Reopen it first. Nothing was {verb}."
		)

	if len(subtasks_of(parent)) >= SUBTASK_CAP:
		raise ToolError(f"{parent} already has {SUBTASK_CAP} steps under it. Nothing was {verb}.")
	return doctype, parent


def _blocking_subtasks(task: str) -> list:
	"""Live children that stand between this task and being finished."""
	if not compat.has_field(FARM_TASK, "parent_task"):
		return []
	return open_subtasks(task)

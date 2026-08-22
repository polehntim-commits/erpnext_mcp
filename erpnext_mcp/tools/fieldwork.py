# SPDX-License-Identifier: MIT
"""The eight calls the Farm Ops app makes all day, with the worker already known.

v0.17.0. Every tool here is a THIN WRAPPER over something Sprint 8 already
shipped. That is the whole design and it is worth defending, because "seven new
tools that call seven old tools" reads like padding until you look at what the
phone would otherwise have to do.

`list_dispatched_tasks` needs `worker_id` — an Employee docname. A phone knows
an email address and a Keychain credential. Somewhere between those two facts
sits a lookup that has to happen on every screen refresh, and there are exactly
two places it can live:

  * **In the app.** Which means every mobile client — this one, the next one,
    whatever somebody writes in three years — reimplements "resolve my login to
    my Employee record", gets the edge case wrong when a worker has no Employee
    row yet, and caches the answer until somebody's transfer breaks it.
  * **Here.** Once, next to the identity capture that makes it possible, tested.

So: the wrappers exist so the app can ask "what am I holding" instead of "what
is EMP-0042 holding", and so it never has to know it is EMP-0042 at all.

THE WRAPPERS ADD EXACTLY THREE THINGS AND NOTHING ELSE:

  1. **The worker, resolved from the authenticated request.** Not from an
     argument. `mobile.resolve_context_user` refuses a request that authenticates
     as one person and asks to act as another — see its docstring, that refusal
     is the difference between scoping and decoration.
  2. **A mobile-shaped payload.** `get_task_with_evidence_contract` returns the
     evidence contract as a list of checklist items with a `satisfied` flag,
     because that is a screen; `get_farm_task` returns the same facts as an
     audit record, because that is a report. Same data, two readers.
  3. **The company filter the phone would otherwise have to guess.** A worker's
     entity access is already enforced by Frappe's User Permissions, and these
     tools apply the same list to their own queries so the counts they report
     agree with what the framework would have returned anyway.

THEY ADD NO NEW REFUSALS AND THEY WEAKEN NONE. `claim_task_via_mobile` is
`claim_farm_task` with the worker filled in: the concurrent-claim limit, the
refusal to self-pick Dispatched work and the refusal of a Draft all still come
from the same code, because it IS the same code. A wrapper that reimplemented
any of those would be a second set of rules to keep in step, and they would drift.

WHY EACH ONE HAS ITS OWN SWITCH ANYWAY. `list_my_tasks` could have been a flag
on `list_dispatched_tasks`. It is not, because an operator running a pilot wants
to enable the four mobile read tools and nothing else — and a flag inside a tool
is not something a switch can reach.

────────────────────────────────────────────────────────────────────────────
WHAT "MY SKILLS" MEANS, AND WHY IT IS HONEST ABOUT NOT KNOWING
────────────────────────────────────────────────────────────────────────────

`list_available_for_me` is meant to show the pool matching the worker's skills.
Sprint 8 put `skill_required` on the task and nothing anywhere records what a
worker HAS: there is no skill register on Employee, in this app or in Frappe HR.

Three ways that could have gone. Invent a register — out of scope for a release
about scoping, and a register nobody populates is worse than none. Guess from
`designation` — a job title is not a licence, and a tool that quietly filtered a
spraying task away from somebody because their title said "Harvest Crew" would
be hiding work with no way to tell. Or say so.

It says so. An explicit `skill` argument filters. A site that has added its own
skills field to Employee is read. Otherwise the whole pool for the worker's
entities comes back, and `skill_matching` in the result says which of the three
happened. An unfiltered pool that admits it is unfiltered is a usable screen; a
filtered pool that cannot say what it filtered on is not.

────────────────────────────────────────────────────────────────────────────
THE EIGHTH TOOL, AND WHY IT IS A THIRD READER RATHER THAN A FLAG
────────────────────────────────────────────────────────────────────────────

v0.117.0. `list_tasks_by_location` answers a question neither `list_my_tasks`
nor `list_available_for_me` answers on its own: "if I drove to MC-Cabin-01
right now, what could I get done in one visit?" A worker planning a morning
does not think in two lists — the three tasks they are already holding at a
cabin and the two more sitting in the pool for it are one trip, and a screen
that made them cross-reference two flat lists to see that would be asking the
phone to do the grouping a server call can do once.

It is a THIRD READER of the same two calls, not a new query. Both
`dispatch.list_available_tasks` and `dispatch.list_dispatched_tasks` are
called exactly as `list_available_for_me` and `list_my_tasks` call them, and
every refusal and every scoping rule this module's other seven tools already
carry — entity access, the honest skill-matching story — travels with them
unchanged. WHY A NEW TOOL AND NOT A `group_by_location` FLAG ON THE EXISTING
TWO, the way the module docstring above already answers for
`list_my_tasks` vs. `list_dispatched_tasks`: an operator piloting the grouped
view wants a switch that reaches it alone, and a flag buried inside a tool
that is already on is not something a switch can reach.

A TASK WITH NO LOCATION IS REPORTED, NOT DROPPED. `unlocated_tasks` carries
every task this call could not place — hand-raised work with no asset or
field named on it — because folding those into a fake "Unlocated" group would
read as a place, and there is no such place. `total_estimated_minutes` is
summed only from tasks that carry an estimate; `tasks_missing_estimate` says
how many in the group did not, so the minutes are read as a floor and not a
promise.
"""

from __future__ import annotations

import frappe

from .. import compat, roles
from ..args import as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import calendar as compliance_calendar
from . import dispatch, mobile

EMPLOYEE = "Employee"

#: Fields a site might keep a worker's skills in, in the order they are
#: believed. None of them is created by this app — see the module docstring on
#: why inventing a register was the wrong answer.
_SKILL_FIELDS = ("farm_skills", "skills", "custom_farm_skills")


# ── shared ──────────────────────────────────────────────────────────────────
def _me(args: dict) -> dict:
	"""Who is calling, which Employee they are, and what they may see.

	One function, so every tool in this module agrees about all three. Raises a
	ToolError naming the fix when the request carries no identity at all — which
	for a mobile client means its credential is missing or was revoked, and that
	is exactly the message it should show.
	"""
	user, source = mobile.resolve_context_user(args)
	if not user:
		raise ToolError(
			"this request carries no per-user identity, so there is no 'me' to answer for. A "
			"mobile client sends `Authorization: token <api_key>:<api_secret>` ALONGSIDE the "
			"X-MCP-Token header — generate_api_token produces the pair. An operator calling from "
			"a desktop client can pass `user` explicitly instead."
		)

	employee = _employee_for(user)
	return {
		"user": user,
		"identity_source": source,
		"employee": employee,
		"entity_access": roles.companies_for(user),
		"preferred_company": roles.default_company_for(user),
		"roles": roles.roles_of(user),
	}


def _employee_for(user: str) -> str:
	if not compat.doctype_exists(EMPLOYEE):
		return ""
	try:
		return str(frappe.db.get_value(EMPLOYEE, {"user_id": user}, "name") or "")
	except Exception:  # pragma: no cover - an Employee table without user_id
		return ""


def _require_employee(me: dict) -> str:
	"""The Employee docname, or a refusal that says how to fix it.

	A Farm Task Assignment names an Employee, not a login. That is right — the
	dispatch board and the payroll register have to be talking about the same
	person — and it means a login with no Employee row cannot hold work. The
	refusal says so plainly rather than returning an empty list, which would
	present to a worker as "there is nothing for me today".
	"""
	if me["employee"]:
		return me["employee"]
	raise ToolError(
		f"{me['user']} has no Employee record on this site, and a Farm Task Assignment names an "
		"Employee rather than a login — the dispatch board and the payroll register have to be "
		"about the same person. Set `user_id` on their Employee record to this email address. "
		"Returning an empty list instead would read on a phone as 'nothing to do today', which "
		"is a different and much worse answer."
	)


def _company_for(me: dict, args: dict) -> str:
	"""Which entity to scope a query to: what was asked for, else the default one.

	A `company` argument outside the worker's entity access is REFUSED rather
	than ignored. Frappe's User Permissions would have returned nothing for it
	anyway, and an empty list is indistinguishable from a quiet day — a refusal
	that names the entity is a fact somebody can act on.
	"""
	wanted = as_str(args, "company")
	if wanted:
		resolved = resolve_company(wanted, required=False)
		if not resolved:
			raise ToolError(f"{wanted!r} is not a Company on this site.")
		if me["entity_access"] and resolved not in me["entity_access"]:
			raise ToolError(
				f"{me['user']} has no access to {resolved!r}. Their entity access is "
				f"{', '.join(me['entity_access'])}. Frappe's User Permissions would have returned "
				"an empty result for this, and an empty result reads as 'nothing there' rather "
				"than 'not yours'."
			)
		return resolved
	return me["preferred_company"] or ""


def _me_block(me: dict) -> dict:
	return {
		"user": me["user"],
		"employee": me["employee"] or None,
		"identity_source": me["identity_source"],
		"entity_access": me["entity_access"],
		"preferred_company": me["preferred_company"] or None,
		"mobile_roles": me["roles"],
	}


# ── 1. list_my_tasks ────────────────────────────────────────────────────────
def list_my_tasks(args: dict) -> ToolResult:
	"""What the caller is holding right now: claimed and in progress, with the job."""
	me = _me(args)
	worker = _require_employee(me)

	inner = {"worker_id": worker, "limit": args.get("limit")}
	state = as_str(args, "state_filter") or as_str(args, "state")
	if state:
		inner["state"] = state
	if as_str(args, "include_finished"):
		inner["include_finished"] = args.get("include_finished")
	elif args.get("include_finished") is not None:
		inner["include_finished"] = args.get("include_finished")
	company = _company_for(me, args)
	if company:
		inner["company"] = company

	result = dispatch.list_dispatched_tasks(inner)
	data = dict(result.data)
	data["me"] = _me_block(me)
	data["company"] = company or None
	data["next"] = _next_actions(data.get("assignments") or [])
	data["note"] = (
		"Claimed means it is yours and not started; In-Progress means the clock is running on "
		"it. `next` says which tool each assignment is waiting for, so a screen can render the "
		"right button without deciding the rule itself."
	)
	return ToolResult(data=data, summary=result.summary)


def _next_actions(assignments: list) -> list:
	"""What each held assignment is waiting for. The button the screen draws."""
	out = []
	for entry in assignments:
		state = entry.get("state")
		if state == "Claimed":
			action = {"tool": "start_task_via_mobile", "label": "Start", "why": "claimed but not started"}
		elif state == "In-Progress":
			action = {
				"tool": "complete_task_via_mobile",
				"label": "Complete",
				"why": "the clock is running on this one",
			}
		else:
			continue
		out.append(
			{
				"assignment": entry.get("name"),
				"task": entry.get("task"),
				"task_name": entry.get("task_name"),
				**action,
			}
		)
	return out


# ── 2. list_available_for_me ────────────────────────────────────────────────
def list_available_for_me(args: dict) -> ToolResult:
	"""The pool the caller could take from, and whether they have a slot free."""
	me = _me(args)
	worker = _require_employee(me)
	company = _company_for(me, args)

	skill, skill_matching = _skill_for(me, args)
	inner = {"worker_id": worker, "limit": args.get("limit")}
	if company:
		inner["company"] = company
	if skill:
		inner["skill"] = skill
	location = as_str(args, "location_filter") or as_str(args, "location")
	if location:
		inner["location"] = location
	for key in ("task_type", "urgency"):
		if as_str(args, key):
			inner[key] = args.get(key)

	result = dispatch.list_available_tasks(inner)
	data = dict(result.data)
	data["me"] = _me_block(me)
	data["company"] = company or None
	data["skill_filter"] = skill or None
	data["skill_matching"] = skill_matching
	data["location_filter"] = location or None
	return ToolResult(data=data, summary=result.summary)


def _skill_for(me: dict, args: dict) -> tuple:
	"""The skill to filter the pool on, and an honest account of where it came from.

	See the module docstring. Three outcomes, all of them reported: an explicit
	argument, a skills field this site happens to have on Employee, or nothing —
	in which case the whole pool comes back and says so.
	"""
	explicit = as_str(args, "skill") or as_str(args, "skill_required")
	if explicit:
		return explicit, f"the `skill` argument ({explicit!r})"

	employee = me.get("employee")
	if employee and compat.doctype_exists(EMPLOYEE):
		field = compat.first_field(EMPLOYEE, *_SKILL_FIELDS)
		if field:
			value = str(frappe.db.get_value(EMPLOYEE, employee, field) or "").strip()
			if value:
				return value, f"this site's Employee.{field} ({value!r})"
			return "", (
				f"this site has Employee.{field} but it is empty for {employee}, so the whole "
				"pool is shown. Fill it in to narrow it."
			)

	return "", (
		"NOT FILTERED. Nothing on this site records what skills a worker has — there is no "
		"skill register on Employee, in this app or in Frappe HR — so the whole pool for this "
		"worker's entities is shown. Pass `skill` to narrow it. Guessing from a job title was "
		"the alternative, and a tool that hid a spraying task from somebody because their title "
		"said 'Harvest Crew' would be hiding work with no way to tell."
	)


# ── 3. get_task_with_evidence_contract ──────────────────────────────────────
def get_task_with_evidence_contract(args: dict) -> ToolResult:
	"""One task shaped for a screen: the job, the checklist, and what is still short."""
	me = _me(args)
	task_name = as_str(args, "task_name") or as_str(args, "task", required=True)

	result = dispatch.get_farm_task({"task": task_name})
	# `get_farm_task` returns the described task AT TOP LEVEL with its history
	# alongside it, so the task fields are everything that is not history. Split
	# rather than re-query: a second read could disagree with the first.
	inner = dict(result.data)
	task = {key: value for key, value in inner.items() if key not in _HISTORY_KEYS}
	contract = task.get("evidence_required") or {}

	if me["entity_access"] and task.get("company") and task["company"] not in me["entity_access"]:
		raise ToolError(
			f"{task_name} belongs to {task['company']}, and {me['user']} has access to "
			f"{', '.join(me['entity_access'])}. Nothing was returned."
		)

	live = inner.get("live_assignment") or {}
	mine = bool(live and me["employee"] and live.get("assigned_to") == me["employee"])
	filed = _filed_evidence(inner)

	checklist = []
	for key in sorted(contract):
		if not contract.get(key):
			continue
		checklist.append(
			{
				"requirement": key,
				"means": _EVIDENCE_MEANS.get(key, key),
				"satisfied": bool(filed.get(key)),
				"capture": _CAPTURE_HINT.get(key, "unknown"),
			}
		)
	outstanding = [item["requirement"] for item in checklist if not item["satisfied"]]

	return ToolResult(
		data={
			"me": _me_block(me),
			"task": task,
			"is_mine": mine,
			"live_assignment": live or None,
			"evidence_contract": checklist,
			"evidence_outstanding": outstanding,
			"evidence_complete": not outstanding,
			"evidence_filed": filed,
			"state": task.get("state"),
			"next": _task_next(task, live, mine, outstanding),
			"produced_record": task.get("produced_record"),
			"source_alert": task.get("source_alert"),
			"alert": inner.get("alert"),
			"history": inner.get("assignments"),
			"note": (
				"`evidence_contract` is the same contract get_farm_task returns, turned into a "
				"checklist a screen can draw. `capture` says which part of the app collects each "
				"one. Files go up through stage_file_chunk / commit_staged_file first, and their "
				"docnames go into complete_task_via_mobile."
			),
		},
		summary=(
			f"{task_name}: {task.get('state')}, "
			f"{len(checklist) - len(outstanding)}/{len(checklist)} evidence requirement(s) met"
			+ ("; yours" if mine else "")
		),
	)


#: The keys `get_farm_task` adds around the task itself. Everything else in its
#: payload is a field of the task.
_HISTORY_KEYS = (
	"assignments",
	"assignment_count",
	"rejections",
	"live_assignment",
	"alert",
	"loop_closed",
	"mcp_action_log_id",
)

#: What each evidence key asks for, in the words a worker reads on a phone.
_EVIDENCE_MEANS = {
	"photos": "at least one photograph of what you found",
	"signature": "a signature capture",
	"findings_text": "what you saw, in words — an empty note records that nothing was wrong",
	"witness": "the name of somebody else who was there",
}

#: Which part of the app collects each one.
_CAPTURE_HINT = {
	"photos": "camera",
	"signature": "signature pad",
	"findings_text": "text field",
	"witness": "text field",
}


def _filed_evidence(inner: dict) -> dict:
	"""What the live assignment has already got against each requirement.

	Read off the LIVE assignment only. Evidence on a rejected earlier attempt is
	real history and belongs in `history`, but it is not evidence for the walk
	somebody is doing now — counting it would mark a requirement satisfied that
	nobody has satisfied.
	"""
	live = inner.get("live_assignment") or {}
	evidence = []
	for entry in inner.get("assignments") or []:
		if entry.get("name") and entry.get("name") == live.get("name"):
			evidence = entry.get("evidence") or []
			break
	kinds = {str(item.get("evidence_type") or "").lower() for item in evidence if isinstance(item, dict)}
	return {
		"photos": "photo" in kinds,
		"signature": bool(live.get("signature_file")) or "signature" in kinds,
		"findings_text": live.get("findings_text") is not None,
		"witness": bool(live.get("witness")),
		"files": len(evidence),
	}


def _task_next(task: dict, live: dict, mine: bool, outstanding: list) -> dict:
	state = task.get("state")
	if state == "Available" and not live:
		return {"tool": "claim_task_via_mobile", "label": "Claim", "why": "it is in the pool"}
	if not mine:
		return {"tool": None, "label": None, "why": "somebody else is holding this one"}
	if live.get("state") == "Claimed":
		return {"tool": "start_task_via_mobile", "label": "Start", "why": "yours, not started"}
	if live.get("state") == "In-Progress":
		return {
			"tool": "complete_task_via_mobile",
			"label": "Complete" if not outstanding else f"Complete ({len(outstanding)} to collect)",
			"why": "the clock is running",
		}
	return {"tool": None, "label": None, "why": f"nothing to do from state {state}"}


# ── 4. list_compliance_calendar_for_me ──────────────────────────────────────
def list_compliance_calendar_for_me(args: dict) -> ToolResult:
	"""The compliance calendar, narrowed to the entities the caller may see.

	ONE CALL PER ENTITY RATHER THAN ONE UNFILTERED CALL. `get_compliance_calendar`
	takes one company, and asking it once with no company would return alerts
	across every entity on the site — which Frappe's User Permissions would then
	filter for a Desk user and NOT filter here, because this app reads through
	`frappe.db.get_all`, which does not consult permissions. So the scoping has
	to be explicit, and it is.
	"""
	me = _me(args)
	entities = me["entity_access"]
	if not entities:
		raise ToolError(
			f"{me['user']} has no Company User Permission, which in Frappe means unrestricted — "
			"so there is no 'my entities' to narrow the calendar to, and returning the whole "
			"site's calendar under a name like this one would be a lie. Scope the account with "
			"create_mobile_user (update_existing=true)."
		)

	only = as_str(args, "company")
	if only:
		entities = [_company_for(me, args)]

	# CHECKED HERE, BEFORE THE LOOP, even though `get_compliance_calendar` checks
	# it too. Inside the loop every exception becomes a named failure for one
	# entity rather than an error — which is right for a broken register and
	# exactly wrong for a typo in the argument, because a worker with three
	# entities would get three "failures" and an empty calendar instead of being
	# told the regime is not one this app knows.
	compliance_calendar.validate_regime(args)

	per_entity, alerts_out, failures = [], [], []
	for company in entities:
		inner = {"company": company}
		# `regime` (v0.19.2) forwards like every other filter rather than being
		# reimplemented here — `get_compliance_calendar` refuses an unrecognised
		# one by name, and a second copy of that check is a second thing to get
		# out of step with the vocabulary.
		for key in ("severity_min", "days_ahead", "category", "alert_type", "regime", "as_of", "limit"):
			if args.get(key) is not None:
				inner[key] = args.get(key)
		try:
			result = compliance_calendar.get_compliance_calendar(inner)
		except Exception as exc:
			# One entity's calendar failing must not take the others down. A
			# worker with three entities and one broken register should see two
			# calendars and a named failure, not an error page.
			failures.append({"company": company, "reason": f"{type(exc).__name__}: {exc}"})
			continue
		data = dict(result.data)
		# `get_compliance_calendar` groups by category — the flat list a phone
		# scrolls is what comes out of the groups, with the entity stamped on
		# each row so a merged list can still say which company an item is from.
		for group in (data.get("by_category") or {}).values():
			for alert in group.get("alerts") or []:
				alert = dict(alert)
				alert["company"] = alert.get("company") or company
				alerts_out.append(alert)
		per_entity.append(
			{
				"company": company,
				"count": int(data.get("alert_count") or 0),
				"by_severity": data.get("by_severity"),
				"overdue": int(data.get("overdue_count") or 0),
				"by_category": {
					key: group.get("count") for key, group in (data.get("by_category") or {}).items()
				},
				"summary": result.summary,
			}
		)

	order = {"Critical": 0, "Warning": 1, "Info": 2}
	alerts_out.sort(
		key=lambda alert: (
			order.get(alert.get("severity"), 9),
			str(alert.get("due_date") or "9999-12-31"),
			str(alert.get("name") or ""),
		)
	)
	critical = [alert for alert in alerts_out if alert.get("severity") == "Critical"]
	overdue = [alert for alert in alerts_out if alert.get("overdue")]

	return ToolResult(
		data={
			"me": _me_block(me),
			"entities": entities,
			"per_entity": per_entity,
			"count": len(alerts_out),
			"critical": len(critical),
			"overdue": len(overdue),
			"alerts": alerts_out,
			"failed_entities": failures,
			"note": (
				"One call per entity, merged and re-sorted Critical first. An entity whose "
				"calendar could not be read is named in `failed_entities` rather than silently "
				"contributing nothing — a clean calendar and an unread one look identical "
				"otherwise."
			),
		},
		summary=(
			f"{len(alerts_out)} alert(s) across {len(entities)} entity/entities: "
			f"{len(critical)} critical, {len(overdue)} overdue"
			+ (f"; {len(failures)} entity/entities could not be read" if failures else "")
		),
	)


# ── 5. claim_task_via_mobile ────────────────────────────────────────────────
def claim_task_via_mobile(args: dict) -> ToolResult:
	"""Take one task from the pool, as the authenticated caller."""
	me = _me(args)
	worker = _require_employee(me)
	task = as_str(args, "task_name") or as_str(args, "task", required=True)

	result = dispatch.claim_farm_task({"task": task, "worker_id": worker})
	data = dict(result.data)
	data["me"] = _me_block(me)
	data["next"] = {"tool": "start_task_via_mobile", "label": "Start", "why": "claimed"}
	return ToolResult(data=data, summary=result.summary, docstatus_delta=result.docstatus_delta)


# ── 6. start_task_via_mobile ────────────────────────────────────────────────
def start_task_via_mobile(args: dict) -> ToolResult:
	"""Clock in on one claimed task, as the authenticated caller."""
	me = _me(args)
	worker = _require_employee(me)

	inner = {"worker_id": worker}
	assignment = as_str(args, "assignment_name") or as_str(args, "assignment")
	if assignment:
		inner["assignment"] = assignment
	else:
		inner["task"] = as_str(args, "task_name") or as_str(args, "task", required=True)
	if as_str(args, "started_at"):
		inner["started_at"] = args.get("started_at")

	result = dispatch.start_farm_task(inner)
	data = dict(result.data)
	data["me"] = _me_block(me)
	data["next"] = {"tool": "complete_task_via_mobile", "label": "Complete", "why": "the clock is running"}
	return ToolResult(data=data, summary=result.summary, docstatus_delta=result.docstatus_delta)


# ── 7. complete_task_via_mobile ─────────────────────────────────────────────
def complete_task_via_mobile(args: dict) -> ToolResult:
	"""Finish one task: file the evidence, write the compliance record.

	`evidence` is the mobile spelling of `evidence_files`; both are accepted, and
	both mean the same thing — File docnames from `commit_staged_file`, or file
	URLs, or objects carrying a caption. THE FILES GO UP FIRST, through
	`stage_file_chunk`; this call carries their names, not their bytes.

	NOTE WHAT IS **NOT** RE-IMPLEMENTED HERE. The evidence contract check, the
	refusal of a completion filed by somebody who was not there, the
	Awaiting-Review routing when the record found something — all of it stays in
	`complete_farm_task`, because it IS `complete_farm_task`. A wrapper with its
	own copy of those rules would be a second set to keep in step, and the
	compliance ones are exactly the set that must never drift.
	"""
	me = _me(args)
	worker = _require_employee(me)

	inner = {"worker_id": worker}
	assignment = as_str(args, "assignment_name") or as_str(args, "assignment")
	if assignment:
		inner["assignment"] = assignment
	else:
		inner["task"] = as_str(args, "task_name") or as_str(args, "task", required=True)

	evidence = args.get("evidence")
	if evidence is None:
		evidence = args.get("evidence_files")
	if evidence is not None:
		inner["evidence_files"] = evidence

	# findings_text is passed THROUGH THE PRESENCE TEST, not through truthiness.
	# An empty string is a positive statement — "I looked and nothing was wrong" —
	# and `complete_farm_task` distinguishes it from the argument being absent,
	# which records that nobody was asked. A wrapper that dropped a falsy value
	# would silently turn the first into the second.
	if "findings_text" in args:
		inner["findings_text"] = args.get("findings_text")

	# `clean_pass` goes through the SAME presence test, for the same reason.
	# v0.17.1: it is the worker's own answer to "was this walk clean", and
	# `complete_farm_task` treats it as authoritative rather than parsing the
	# findings text for intent. Absent means nobody was asked, which is a third
	# state and not a synonym for false — see `dispatch.clean_pass_flag`.
	if "clean_pass" in args:
		inner["clean_pass"] = args.get("clean_pass")

	# v0.20.1. `visit_id` joins the pass-through list. It is the handset's own
	# identifier for one trip — five cabins closed on one walk to the north block
	# is one visit and five completions.
	#
	# IT IS FORWARDED, NOT CHECKED HERE, AND IT IS NOW CHECKED. The shape is
	# confirmed — a UUID, 8-4-4-4-12 — and `args.as_visit_id` refuses anything
	# else where `complete_farm_task` writes the column, which is the one place
	# that can be true for every door into it: this wrapper, the REST endpoint
	# behind it, and a direct tool call. A copy of the rule here would be a second
	# one to keep in step for no reach it does not already have.
	#
	# The lenient reading this replaces was that refusing a completion over its
	# least important field is worse than storing a value nobody can use. It is
	# not: `list_visits` groups on exact value, so a garbled identifier does not
	# read as a bad row, it reads as a SECOND VISIT — and the rollup somebody
	# reports off is wrong rather than obviously incomplete.
	# v0.69.0. `materials_used` joins the pass-through list and nothing about it
	# is re-implemented here — the parse, the refusal, the drawdown and the two
	# interval windows all stay in `complete_farm_task`, for the reason the
	# docstring above gives about the evidence contract. A spray task whose tank
	# mix is already on the task needs nothing from the phone at all: the mix is
	# drawn down from the task itself. This argument is for the OTHER case — the
	# twine, the filter, the bin liners — and for an applicator correcting what
	# actually went in the tank.
	for key in (
		"signature_file",
		"completion_narrative",
		"witness",
		"farm_location_gps",
		"actual_duration_minutes",
		"completed_at",
		"record_data",
		"visit_id",
		"materials_used",
	):
		if args.get(key) is not None:
			inner[key] = args.get(key)

	result = dispatch.complete_farm_task(inner)
	data = dict(result.data)
	data["me"] = _me_block(me)
	return ToolResult(data=data, summary=result.summary, docstatus_delta=result.docstatus_delta)


# ── 8. list_tasks_by_location ───────────────────────────────────────────────
#: Worst-to-least-urgent, the same order `dispatch._by_urgency` sorts a flat
#: board by. Duplicated rather than imported: it is four literal strings, and
#: reaching into a leading-underscore name in another module for them would
#: tie this file to dispatch.py's internals rather than its public calls.
_URGENCY_ORDER = {"Critical": 0, "High": 1, "Normal": 2, "Low": 3}


def list_tasks_by_location(args: dict) -> ToolResult:
	"""One trip's worth of work per place: what is held plus what could be taken.

	Combines `list_my_tasks` (Claimed/In-Progress — already the caller's) with
	`list_available_for_me` (the self-pickable pool), then groups both by the
	Farm Task's own `location`/`location_doctype` rather than reporting them as
	two lists a screen has to cross-reference. See the module docstring for why
	this is a third reader of the same two calls and not a new query.
	"""
	me = _me(args)
	worker = _require_employee(me)
	company = _company_for(me, args)

	skill, skill_matching = _skill_for(me, args)
	pool_inner = {"worker_id": worker, "limit": args.get("limit")}
	if company:
		pool_inner["company"] = company
	if skill:
		pool_inner["skill"] = skill
	for key in ("task_type", "urgency"):
		if as_str(args, key):
			pool_inner[key] = args.get(key)
	pool = dispatch.list_available_tasks(pool_inner).data.get("tasks") or []

	held_inner = {"worker_id": worker, "include_finished": False, "limit": args.get("limit")}
	if company:
		held_inner["company"] = company
	held = dispatch.list_dispatched_tasks(held_inner).data.get("assignments") or []

	# Combined and de-duplicated by task name. The two sources cannot name the
	# same task — the pool is Available and held work is Claimed/In-Progress —
	# but a dict keyed by name costs nothing and refuses to double-count a task
	# if that ever stops being true.
	combined = {}
	for entry in pool:
		name = entry.get("name")
		if name:
			combined[name] = {**entry, "held": False, "assignment": None, "assignment_state": None}
	for entry in held:
		detail = entry.get("task_detail")
		if not detail or not detail.get("name"):
			continue
		combined[detail["name"]] = {
			**detail,
			"held": True,
			"assignment": entry.get("name"),
			"assignment_state": entry.get("state"),
		}

	location = as_str(args, "location_filter") or as_str(args, "location")
	tasks = list(combined.values())
	if location:
		wanted = location.strip().casefold()
		tasks = [task for task in tasks if str(task.get("location") or "").strip().casefold() == wanted]

	groups, unlocated = {}, []
	for task in tasks:
		place = str(task.get("location") or "").strip()
		if not place:
			unlocated.append(_brief(task))
			continue
		key = (str(task.get("location_doctype") or ""), place)
		groups.setdefault(key, []).append(task)

	described = [_describe_group(place, doctype, members) for (doctype, place), members in groups.items()]
	described.sort(
		key=lambda group: (
			min(_URGENCY_ORDER.get(task["urgency"], 9) for task in group["tasks"]),
			-group["total_tasks"],
			group["location"],
		)
	)

	data = {
		"me": _me_block(me),
		"company": company or None,
		"skill_filter": skill or None,
		"skill_matching": skill_matching,
		"location_filter": location or None,
		"location_groups": described,
		"group_count": len(described),
		"total_tasks": len(tasks),
		"unlocated_tasks": unlocated,
		"unlocated_count": len(unlocated),
		"note": (
			"`held` tasks are already the caller's (Claimed or In-Progress); the rest are self-"
			"pickable and sitting in the pool. `total_estimated_minutes` sums only tasks that "
			"carry an estimate — `tasks_missing_estimate` says how many in the group did not, so "
			"the minutes read as a floor and not a promise."
		),
	}
	if unlocated:
		data["unlocated_note"] = (
			f"{len(unlocated)} task(s) here name no location — hand-raised work with no asset or "
			"field on it — and are reported here rather than folded into a fake group. There is "
			"no place called 'Unlocated'."
		)

	summary = f"{len(described)} location(s) covering {len(tasks)} task(s) for {worker}"
	if unlocated:
		summary += f"; {len(unlocated)} task(s) with no location"
	return ToolResult(data=data, summary=summary)


def _brief(task: dict) -> dict:
	"""A task, trimmed to what a location-grouped screen draws per row."""
	return {
		"name": task.get("name"),
		"task_name": task.get("task_name"),
		"task_type": task.get("task_type"),
		"state": task.get("state"),
		"urgency": task.get("urgency"),
		"estimated_duration_minutes": task.get("estimated_duration_minutes"),
		"held": task.get("held", False),
		"assignment": task.get("assignment"),
		"self_pickable": task.get("self_pickable", False),
	}


def _describe_group(place: str, location_doctype: str, members: list) -> dict:
	briefs = [_brief(task) for task in members]
	estimated = [task["estimated_duration_minutes"] for task in briefs if task["estimated_duration_minutes"]]
	total_tasks = len(briefs)
	total_minutes = sum(estimated)
	held_count = len([task for task in briefs if task["held"]])
	label = f"{place}: {total_tasks} task{'s' if total_tasks != 1 else ''}"
	if total_minutes:
		label += f", ~{total_minutes} min"
	return {
		"location": place,
		"location_doctype": location_doctype or None,
		"label": label,
		"total_tasks": total_tasks,
		"held_count": held_count,
		"available_count": total_tasks - held_count,
		"total_estimated_minutes": total_minutes,
		"tasks_missing_estimate": total_tasks - len(estimated),
		"tasks": briefs,
	}

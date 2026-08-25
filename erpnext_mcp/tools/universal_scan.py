# SPDX-License-Identifier: MIT
"""One scan, whatever was on the tag.

THE PROBLEM THIS SOLVES IS ON THE HANDSET, NOT ON THE SERVER. Farm Ops already
had four scanners: a badge step that resolves a card to a picker, an asset step
that resolves a QR to a valve, and — reachable only by navigating to the right
screen first — a housing and a block lookup. Every one of them works, and every
one of them requires the person holding the phone to know what they are about to
scan BEFORE they scan it. In an orchard that is exactly backwards. A worker
walks up to a thing with a sticker on it; which of the four registers that
sticker belongs to is the question, not the premise.

So this is one call that takes the raw string a camera produced and answers it.
The cascade is ordered, first match wins, and the order is not arbitrary:

    1. EMPLOYEE BADGE, because a badge is the only tag on this farm attached to
       a PERSON, and mis-reading a person as a valve is the one confusion with a
       payroll consequence. It is also the narrowest test — the badge register
       is keyed on the badge ID itself — so putting it first costs one exists().
    2. ASSET TAG, the largest register and the only branch that WRITES.
    3. HOUSING UNIT, then
    4. FIELD/BLOCK, both read-only and both keyed on their own docname.
    5. UNKNOWN, which is a real answer and not a failure. See below.

WHY UNKNOWN IS AN ANSWER RATHER THAN AN ERROR. The scan that resolves to
nothing is the interesting one: a supplier's carton barcode, a hand-written
label, a tag printed by a system this app has never heard of. A refusal would
send the worker back to a menu; `entity_type: "unknown"` with the raw content
and `create_task` in `available_actions` lets the handset offer the only thing
that is useful at that moment — "raise a job about whatever this is" — and keeps
the string so the task can carry it.

THE ONE SCAN THAT IS REFUSED RATHER THAN ANSWERED is a credential document —
`generate_mobile_login_qr`'s own `{"url":…,"api_key":…,"api_secret":…}`, which is
what a camera reads when the nearest QR is the wrong one. Everything above about
`unknown` is exactly why it has to be refused HERE and not left to fall through:
the unknown branch's whole promise is that the scan comes back whole so a task
can carry it, and for that one string keeping it whole means handing a live
credential to a screen, a task description and a handset cache. The refusal names
what was scanned without repeating it.

THE GOVERNMENT-ID CASE IS DELIBERATELY ABSENT. A driving licence's PDF417 and a
passport's MRZ are parsed ON THE HANDSET and routed to the hiring wizard there.
That is not an oversight and not a gap to fill later: those barcodes carry a
date of birth, a document number and an address, and posting them to a server so
it can tell the app what it already knows would put a person's identity document
into an HTTP body, an audit row's arguments and a log file, for no answer the
phone did not already have. The cascade below has four branches because there
are four registers; identity documents are not one of them.

WHAT IS WRITTEN, AND WHERE. Exactly one branch mutates: an asset scan updates
`last_scan_at`, `last_scan_by` and — where the handset sent a fix — the asset's
GPS position, which is `scan_asset`'s existing behaviour and the reason a valve
knows when somebody was last standing in front of it. Nothing else here writes
anything, and `scan_recorded` says which happened rather than leaving a caller
to infer it from the entity type.

TASKS, DUE DATES AND WHY `overdue_tasks` IS A SUBSET. Farm Task carries no due
date of its own — a job is Available or it is not — and the date a compliance
obligation is actually measured against lives on the Compliance Alert the task
answers. So "overdue" here means: a live task whose `source_alert` was due
before today. `overdue_tasks` is a SUBSET of `pending_tasks` rather than a
partition of it, so a client that renders only `pending_tasks` still shows the
late work; every task in both carries `overdue` and `due_date` so nothing has to
be re-derived on the handset.
"""

from urllib.parse import unquote

import frappe

from .. import bucket_bridge, compat
from .. import shifts as shift_register
from ..args import as_int, as_str
from ..erpnext_mcp.doctype.farm_task.farm_task import STATES, TERMINAL_STATES
from ..errors import ToolError
from ..result import ToolResult
from . import asset_actions, asset_tags, badges, farm, housing, spray_rei

ASSET_REGISTER = "Asset Register"
BADGE_MAP = badges.BADGE_DOCTYPE
HOUSING_UNIT = "Housing Unit"
FIELD = "Field"
FARM_SHIFT = "Farm Shift"
FARM_TASK = "Farm Task"
FARM_TASK_ASSIGNMENT = "Farm Task Assignment"
ALERT = "Compliance Alert"

EMPLOYEE = "employee"
ASSET = "asset"
HOUSING = "housing_unit"
BLOCK = "field"
UNKNOWN = "unknown"

#: What the caller may do next, per resolved type. The handset renders these as
#: buttons, so the vocabulary is the CLIENT'S rather than this app's tool names:
#: `create_task` is the button, `create_farm_task` is the method behind it, and
#: keeping the two apart means renaming a tool does not silently rearrange a
#: screen in an orchard.
AVAILABLE_ACTIONS = {
	EMPLOYEE: ("create_task", "view_compliance", "view_i9"),
	ASSET: ("create_task", "log_state_change", "report_issue"),
	HOUSING: ("create_task", "start_inspection", "log_state_change"),
	BLOCK: ("create_task", "view_irrigation"),
	UNKNOWN: ("create_task",),
}

#: The states a task is still work in. Derived from the doctype's own vocabulary
#: rather than listed here, so a state added to Farm Task cannot quietly stop
#: appearing on a scan.
LIVE_STATES = tuple(state for state in STATES if state not in TERMINAL_STATES)

#: Ten, because the panel this fills is a phone screen and the eleventh entry is
#: one nobody scrolls to. `history_limit` raises it for a caller that wants more.
HISTORY_LIMIT = 10
HISTORY_MAX = 100

#: Most tasks or alerts one scan reports. Past this something is wrong with the
#: data rather than busy on the farm, and a scan screen is not where somebody
#: should discover that.
LINKED_CAP = 50

_TASK_FIELDS = (
	"name",
	"task_name",
	"task_type",
	"state",
	"urgency",
	"company",
	"location_doctype",
	"location",
	"asset",
	"assigned_to",
	"assigned_to_name",
	"skill_required",
	"dispatch_mode",
	"source_alert",
	"farm_shift",
	"creation",
)

_ALERT_FIELDS = (
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
	"can_dismiss",
)

#: The timeline for each branch, as (doctype, the column carrying the docname,
#: extra filters, fields wanted). A table rather than four functions because
#: every entry is the same query with different names in it, and because a site
#: that has not migrated one of these doctypes must lose that ROW rather than the
#: whole history — which is what `compat.doctype_exists` and `existing_fields`
#: give for free once the shape is uniform.
_HISTORY = {
	EMPLOYEE: (
		(
			FARM_TASK_ASSIGNMENT,
			"assigned_to",
			{},
			("name", "task", "task_name", "state", "claimed_at", "started_at", "completed_at", "creation"),
		),
	),
	ASSET: (
		(
			"Asset State Log",
			"asset_name",
			{},
			("name", "action", "from_state", "to_state", "performed_by", "performed_at", "creation"),
		),
		(
			"Inspection Session",
			"location",
			{"location_doctype": ASSET_REGISTER},
			("name", "template", "state", "worker_name", "started_at", "submitted_at", "creation"),
		),
	),
	HOUSING: (
		(
			"Housing Inspection",
			"unit",
			{},
			("name", "unit", "inspector_name", "inspection_date", "workflow_state", "creation"),
		),
		(
			"Detector Test",
			"unit",
			{},
			(
				"name",
				"unit",
				"tester_name",
				"test_date",
				"smoke_detector_result",
				"co_detector_result",
				"creation",
			),
		),
		(
			"Inspection Session",
			"location",
			{"location_doctype": HOUSING_UNIT},
			("name", "template", "state", "worker_name", "started_at", "submitted_at", "creation"),
		),
	),
	BLOCK: (
		(
			"Water Test",
			"block",
			{},
			(
				"name",
				"source",
				"test_date",
				"coliform_result",
				"ecoli_result",
				"contamination_detected",
				"creation",
			),
		),
		(
			"Inspection Session",
			"location",
			{"location_doctype": FIELD},
			("name", "template", "state", "worker_name", "started_at", "submitted_at", "creation"),
		),
	),
}


# ── the scanned string ──────────────────────────────────────────────────────
def scan_target(content: str) -> str:
	"""The docname a scan points at. A tag QR encodes a URL, not a bare name.

	`Asset Register.before_save` builds `qr_url` as `<public url>/scan/<name>`
	with the docname percent-encoded, so the string a camera actually produces
	from a printed asset tag is a full URL and matching it against the register
	as-is would resolve nothing. Everything else — a badge ID, a unit name typed
	into the manual-entry box, a supplier's barcode — arrives bare and passes
	through untouched.

	Query and fragment are dropped because a QR read off a curved sticker in the
	sun is often followed by whatever the scanner appended, and a trailing `?`
	has never been part of a docname.
	"""
	text = (content or "").strip()
	marker = "/scan/"
	if marker not in text:
		return text
	tail = text.split(marker, 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/")
	return unquote(tail) if tail else text


#: What makes a scanned STRING a credential document rather than a tag: a JSON
#: object or array naming one of these. The same test `api/guard.redact_payloads`
#: applies to an argument on its way into the audit log, made here because this
#: tool answers with what it was given and the audit strip cannot reach that.
_CREDENTIAL_HINTS = ("api_secret", "api_key", "password", "secret")


def _refuse_credential_payload(text: str) -> None:
	"""A login QR scanned at a scan step is refused, and is NOT quoted back.

	`generate_mobile_login_qr` produces `{"url":…,"api_key":…,"api_secret":…}`,
	and the thing a camera reads by accident is whichever QR is nearest. Without
	this the payload takes the `unknown` branch, where the whole point is that the
	scan comes back whole so a task can carry it — which for this one string means
	handing a live credential to a screen, a task's description and whatever the
	handset caches. `resolve_badge` refuses the same string at the badge step and
	`bucket_bridge` explains why at length; this is that decision at the door in
	front of all four registers.

	NARROW ON PURPOSE, and the same test the audit strip uses: a string has to
	OPEN a JSON document AND name a credential key. An ordinary tag, a docname
	with a brace nowhere near it and a supplier's barcode are untouched, because a
	scanner that refused anything unusual would be a scanner nobody could use.
	"""
	if text[:1] not in ("{", "["):
		return
	lowered = text.lower()
	if not any(hint in lowered for hint in _CREDENTIAL_HINTS):
		return
	raise ToolError(
		f"that scan is a credential document rather than a tag: {bucket_bridge._display(text)}. "
		"It was matched against no register and is not repeated here — a mobile login QR was "
		"almost certainly scanned at a scan step by mistake. Nothing was read and nothing was "
		"changed."
	)


def _content(args: dict) -> str:
	"""The raw scan, under whichever of the four names the caller used.

	`content` is the documented one. The other three are what a camera callback
	is called on the handset, and accepting them costs nothing next to a tool
	that refuses a perfectly good scan over the key it arrived under.
	"""
	for key in ("content", "scan", "raw", "code"):
		value = as_str(args, key)
		if value:
			return value
	return as_str(args, "content", required=True)


# ── shared reads ────────────────────────────────────────────────────────────
def _today() -> str:
	return frappe.utils.today()


def _days_until(due: str, today: str):
	if not due:
		return None
	try:
		return int(frappe.utils.date_diff(due, today))
	except Exception:  # pragma: no cover - an unparseable stored date
		return None


def _describe_task(row: dict, due_dates: dict, today: str) -> dict:
	due = due_dates.get(str(row.get("source_alert") or "")) or None
	remaining = _days_until(due, today)
	return {
		"name": row.get("name"),
		"task_name": row.get("task_name"),
		"task_type": row.get("task_type") or None,
		"state": row.get("state") or None,
		"urgency": row.get("urgency") or "Normal",
		"company": row.get("company") or None,
		"location_doctype": row.get("location_doctype") or None,
		"location": row.get("location") or None,
		"asset": row.get("asset") or None,
		"assigned_to": row.get("assigned_to") or None,
		"assigned_to_name": row.get("assigned_to_name") or row.get("assigned_to") or None,
		"skill_required": row.get("skill_required") or None,
		"dispatch_mode": row.get("dispatch_mode") or "Either",
		"source_alert": row.get("source_alert") or None,
		"farm_shift": row.get("farm_shift") or None,
		"due_date": due,
		"days_until_due": remaining,
		"overdue": bool(remaining is not None and remaining < 0),
		"created_at": str(row.get("creation") or "") or None,
	}


def _alert_due_dates(tasks: list) -> dict:
	"""`{alert docname: due_date}` for every alert the given tasks answer.

	One query for the whole batch rather than one per task: a cabin with eight
	open jobs on it is ordinary, and a scan screen that costs nine round trips
	to the database is the kind of thing that is fine on a laptop and miserable
	on a phone at the end of a row.
	"""
	names = sorted({str(task.get("source_alert") or "") for task in tasks} - {""})
	if not names or not compat.doctype_exists(ALERT) or not compat.has_field(ALERT, "due_date"):
		return {}
	rows = (
		frappe.db.get_all(
			ALERT, filters={"name": ("in", names)}, fields=["name", "due_date"], limit=len(names)
		)
		or []
	)
	return {str(row["name"]): str(row.get("due_date") or "") or None for row in rows}


def _tasks_linked_to(filter_sets: list) -> list:
	"""Every live Farm Task matched by any of these filter sets, deduplicated.

	Several filter sets because one entity is reachable from a task through more
	than one column — an asset through `asset` and through the dynamic
	`location`, a person through `assigned_to` — and a task that matches twice is
	one task. A filter naming a column this site does not have is skipped rather
	than raising: a register that has not migrated is a register with no tasks on
	it, not a broken scan.
	"""
	if not compat.doctype_exists(FARM_TASK):
		return []
	fields = compat.existing_fields(FARM_TASK, _TASK_FIELDS)
	if not fields:
		return []

	seen: dict = {}
	for filters in filter_sets:
		if any(not compat.has_field(FARM_TASK, column) for column in filters):
			continue
		rows = frappe.db.get_all(
			FARM_TASK,
			filters={**filters, "state": ("in", list(LIVE_STATES))},
			fields=fields,
			order_by="creation desc",
			limit=LINKED_CAP,
		)
		for row in rows or []:
			seen.setdefault(str(row.get("name")), dict(row))
	return sorted(seen.values(), key=lambda row: str(row.get("creation") or ""), reverse=True)


def _alerts_for(doctype: str, docname: str, today: str) -> list:
	"""The open compliance alerts raised against one record.

	KEYED ON `source_doctype`/`source_docname`, which is the link the rule engine
	actually writes — every alert names the record its condition was found on.
	Dismissed rows are excluded because a dismissal is somebody with the whole
	picture saying this one does not need doing, and repeating it to a worker
	standing in front of the cabin re-opens a decision that was already made.
	"""
	if not compat.doctype_exists(ALERT):
		return []
	if not compat.has_field(ALERT, "source_doctype") or not compat.has_field(ALERT, "source_docname"):
		return []
	filters = {"source_doctype": doctype, "source_docname": docname}
	if compat.has_field(ALERT, "dismissed"):
		filters["dismissed"] = 0
	rows = frappe.db.get_all(
		ALERT,
		filters=filters,
		fields=compat.existing_fields(ALERT, _ALERT_FIELDS),
		order_by="due_date asc",
		limit=LINKED_CAP,
	)
	out = []
	for row in rows or []:
		due = str(row.get("due_date") or "") or None
		remaining = _days_until(due, today)
		out.append(
			{
				"name": row.get("name"),
				"alert_type": row.get("alert_type"),
				"severity": row.get("severity") or "Warning",
				"category": row.get("category") or "Other",
				"message": row.get("alert_message") or None,
				"due_date": due,
				"days_until_due": remaining,
				"overdue": bool(remaining is not None and remaining < 0),
				"first_seen": str(row.get("first_seen") or "") or None,
				"can_dismiss": bool(frappe.utils.cint(row.get("can_dismiss"))),
			}
		)
	return out


def _history(entity_type: str, docname: str, limit: int) -> list:
	"""The last `limit` things that happened to this record, newest first.

	Ordered on `creation` across every doctype rather than on each row's own date
	column, for the reason `asset_tags._asset_history` is: an inspection dated
	last Tuesday and filed this morning belongs where it was FILED in a list whose
	job is "what has been happening here", and the record's own date is reported
	alongside so a reader can see both.
	"""
	events = []
	for doctype, link_field, extra, wanted in _HISTORY.get(entity_type, ()):
		if not compat.doctype_exists(doctype) or not compat.has_field(doctype, link_field):
			continue
		if any(not compat.has_field(doctype, column) for column in extra):
			continue
		fields = compat.existing_fields(doctype, wanted)
		if not fields:
			continue
		try:
			rows = frappe.db.get_all(
				doctype,
				filters={link_field: docname, **extra},
				fields=fields,
				order_by="creation desc",
				limit=limit,
			)
		except Exception:  # pragma: no cover - a column this site shapes differently
			continue
		for row in rows or []:
			event = {"doctype": doctype, "docname": row.get("name")}
			for field in fields:
				if field != "name":
					event[field] = str(row.get(field) or "") or None
			event["timestamp"] = str(row.get("creation") or "")
			events.append(event)

	events.sort(key=lambda event: event.get("timestamp") or "", reverse=True)
	return events[:limit]


#: What the three state keys say on a branch with no state machine behind it.
#: Named once so the empty answer and the populated one cannot drift into
#: different key sets.
_EMPTY_STATE = {
	"state_asset": None,
	"current_state": None,
	"state_actions": [],
	# v0.77.0. The full menu for the asset's TYPE, of which `state_actions` is
	# the subset that is a legal transition right now. A pre-trip inspection and
	# a calibration record are things a worker does at the machine and neither is
	# a state change, so a screen built from `state_actions` alone could not
	# draw them. Every row says whether it is implemented — see
	# `tools/asset_actions.py` for why that is published rather than filtered.
	"action_menu": [],
}

#: v0.78.0. The status block's own two headline keys, empty. Present on every
#: branch for the reason `_EMPTY_STATE` is: a client draws one banner and reads
#: two keys rather than testing which of five entity types it got back. Only the
#: asset branch fills them — a badge and a cabin have no service schedule, no
#: hour meter and no restricted-entry window of their own.
_EMPTY_STATUS = {
	"status": None,
	"needs_attention": False,
	"warnings": [],
}


def _state_actions(asset_name: str) -> dict:
	"""The concrete state changes this asset can take right now.

	WHY THIS IS NOT `available_actions`. Those are the CLIENT'S vocabulary — five
	fixed strings a handset turns into buttons, and `log_state_change` is one of
	them. It says a state change is offered here; it does not say WHICH, and a
	worker standing at a valve is choosing between "open" and "close", not
	between "log a state change" and nothing. The two lists answer different
	questions and a scan that returned only the first sent the phone back for a
	second round trip to `get_available_actions` before it could draw the screen
	the scan exists to draw.

	POPULATED ON THE ASSET BRANCH ONLY, because it is the only branch whose
	docname is an Asset Register record — the register the state machine and the
	state log both key on. See `_housing_branch` for why a cabin does not carry
	its own actions despite having states.

	KEPT IN THE SHAPE `get_available_actions` RETURNS, field for field, because
	they are the same fact from the same state machine. A screen that renders one
	renders the other, and an action that appears here is one `log_asset_state_change`
	will accept — the transition is validated against the same table when it does.

	Empty rather than absent for an asset type with no state machine, and the
	whole block is skipped where the register has not migrated: a scan is not the
	place to discover that.
	"""
	empty = dict(_EMPTY_STATE)
	if not asset_name or not compat.doctype_exists(ASSET_REGISTER):
		return empty
	if not frappe.db.exists(ASSET_REGISTER, asset_name):
		return empty
	try:
		row = dict(
			frappe.db.get_value(
				ASSET_REGISTER, asset_name, ["name", "asset_type", "current_state"], as_dict=True
			)
			or {}
		)
	except Exception:  # pragma: no cover - a site shaping these columns differently
		return empty

	asset_type = str(row.get("asset_type") or "") or "General"
	defn = asset_tags._STATE_DEFINITIONS.get(asset_type)
	if not defn:
		return {
			**_EMPTY_STATE,
			"state_asset": row.get("name"),
			"action_menu": asset_actions.menu_for(asset_type),
		}

	current = asset_tags._current_state_value(row.get("current_state")) or defn["default"]
	return {
		"state_asset": row.get("name"),
		"current_state": current,
		"state_actions": asset_tags._actions_for(asset_type, current),
		"action_menu": asset_actions.menu_for(asset_type, current),
	}


def _active_shift(employee: str, company: str = "") -> dict | None:
	"""The open shift this person is on right now, or None.

	WALKED IN PYTHON over the open shifts rather than joined through the crew
	table, for the reason `list_shifts` gives: the crew is a child table and a
	join returns one row per crew member. There are single digits of open shifts
	on a farm at any moment, and a foreman who has left the crew (`left_at` set)
	is not on it however open the shift is.
	"""
	if not employee or not compat.doctype_exists(FARM_SHIFT):
		return None
	filters = {"end_datetime": ("is", "not set")}
	if company and compat.has_field(FARM_SHIFT, "company"):
		filters["company"] = company
	try:
		open_shifts = frappe.db.get_all(
			FARM_SHIFT,
			filters=filters,
			fields=compat.existing_fields(
				FARM_SHIFT, ("name", "company", "shift_type", "foreman_name", "location", "start_datetime")
			),
			order_by="start_datetime desc",
			limit=LINKED_CAP,
		)
	except Exception:  # pragma: no cover - a site without the shift columns
		return None

	for shift in open_shifts or []:
		for member in shift_register.crew_of(str(shift.get("name"))) or []:
			if str(member.get("employee")) != employee or member.get("left_at"):
				continue
			return {
				"shift": shift.get("name"),
				"company": shift.get("company") or None,
				"shift_type": shift.get("shift_type") or None,
				"foreman_name": shift.get("foreman_name") or None,
				"location": shift.get("location") or None,
				"started_at": str(shift.get("start_datetime") or "") or None,
				"joined_at": str(member.get("joined_at") or "") or None,
				"role": member.get("role") or None,
			}
	return None


# ── the four probes ─────────────────────────────────────────────────────────
def _employee_branch(candidate: str, args: dict) -> dict | None:
	"""A badge, if the register has one under exactly this string.

	COMMITS ON THE REGISTER ROW, NOT ON THE SHAPE. `resolve_badge` refuses a
	retired badge and one belonging to somebody who has left, and those refusals
	are allowed to propagate here rather than being caught and demoted to
	"unknown": a card that WAS issued is a badge whatever its state, and telling
	the person holding it that the sticker means nothing would be the wrong
	sentence in each of the three cases `resolve_badge` distinguishes.
	"""
	if not compat.doctype_exists(BADGE_MAP) or not frappe.db.exists(BADGE_MAP, candidate):
		return None

	inner = {"badge_id": candidate}
	company = as_str(args, "company")
	if company:
		inner["company"] = company
	shift = as_str(args, "shift") or as_str(args, "farm_shift")
	if shift:
		inner["shift"] = shift

	entity = dict(badges.resolve_badge(inner).data)
	person = str(entity.get("employee") or "")
	entity["active_shift"] = _active_shift(person, str(entity.get("company") or ""))
	entity["on_active_shift"] = entity["active_shift"] is not None

	return {
		"entity_type": EMPLOYEE,
		"entity": entity,
		"docname": person,
		"alert_doctype": "Employee",
		"task_filters": [{"assigned_to": person}] if person else [],
		"scan_recorded": False,
	}


def _asset_branch(candidate: str, args: dict) -> dict | None:
	"""An asset tag. THE ONE BRANCH THAT WRITES — see the module docstring.

	THE ENTITY IS CHECKED BEFORE THE STAMP AND NOT AFTER, which is the one place
	the order matters on this branch. `scan_asset` takes no company of its own —
	it is called from a screen that has already resolved the asset — so a scan
	scoped to one company would otherwise write `last_scan_at` and the worker's
	GPS fix onto another entity's record and only then hand back a row the caller
	may not read. `asset_row` makes exactly the comparison the other three
	branches get from their own tools, and it refuses by naming the entity the
	asset actually belongs to: an operator holding a tag from the wrong farm is
	better told which farm than told nothing.
	"""
	if not compat.doctype_exists(ASSET_REGISTER) or not frappe.db.exists(ASSET_REGISTER, candidate):
		return None

	company = as_str(args, "company")
	if company:
		asset_tags.asset_row(candidate, company)

	inner = {"asset_name": candidate}
	scanned_by = as_str(args, "scanned_by")
	if scanned_by:
		inner["scanned_by"] = scanned_by
	for key in ("gps_lat", "gps_lon", "gps_latitude", "gps_longitude", "history_limit", "timezone"):
		if args.get(key) is not None:
			inner[key] = args[key]

	entity = dict(asset_tags.scan_asset(inner).data)

	return {
		"entity_type": ASSET,
		"entity": entity,
		"docname": candidate,
		"alert_doctype": ASSET_REGISTER,
		"task_filters": [
			{"asset": candidate},
			{"location_doctype": ASSET_REGISTER, "location": candidate},
		],
		"scan_recorded": True,
		# READ AFTER `scan_asset`, not before, so the state reported is the state
		# after this scan's own write rather than the one it replaced.
		"state": _state_actions(candidate),
		# v0.78.0. LIFTED OUT OF THE ENTITY RATHER THAN RECOMPUTED. `scan_asset`
		# already composed the status report — it is the same scan and the same
		# moment — and asking `asset_status` for it a second time here would
		# double every query behind a screen a worker is waiting on.
		"status": {
			"status": entity.get("status"),
			"needs_attention": bool(entity.get("needs_attention")),
			"warnings": entity.get("warnings") or [],
		},
	}


def _housing_branch(candidate: str, args: dict) -> dict | None:
	"""A cabin, a barracks, a wash station."""
	if not compat.doctype_exists(HOUSING_UNIT) or not frappe.db.exists(HOUSING_UNIT, candidate):
		return None

	inner = {"unit": candidate}
	company = as_str(args, "company")
	if company:
		inner["company"] = company

	entity = dict(housing.get_housing_unit(inner).data)
	filters = [{"location_doctype": HOUSING_UNIT, "location": candidate}]
	# A unit with an asset twin carries jobs raised against the TAG as well as
	# against the register row, and a worker in front of the cabin wants both.
	related = str(entity.get("related_asset") or "")
	if related:
		filters.append({"asset": related})

	return {
		"entity_type": HOUSING,
		"entity": entity,
		"docname": candidate,
		"alert_doctype": HOUSING_UNIT,
		"task_filters": filters,
		"scan_recorded": False,
		# NO STATE MACHINE ON THIS BRANCH, and not for want of one. A Housing Unit
		# has states — vacant, occupied, winterized — but they live on an ASSET
		# REGISTER row of type "Housing Unit", and nothing links a unit to one:
		# `Housing Unit.related_asset` points at ERPNext's own fixed-Asset doctype,
		# which is the depreciation record and has no `current_state` on it. So
		# the three keys come back empty here rather than resolving a docname from
		# the wrong register. Scanning the cabin's own asset tag is what carries
		# the actions, and that is the asset branch above.
	}


def _field_branch(candidate: str, args: dict) -> dict | None:
	"""A planted block, with the zones that water it."""
	if not compat.doctype_exists(FIELD) or not frappe.db.exists(FIELD, candidate):
		return None

	inner = {"field": candidate}
	company = as_str(args, "company")
	if company:
		inner["company"] = company

	entity = dict(farm.get_field(inner).data)

	# v0.78.0. THE BRANCH WHERE A RESTRICTED-ENTRY WARNING MATTERS MOST, because
	# this is a worker standing at the gate of the block itself rather than at a
	# machine parked near one. A `Spray REI` names a block directly, so no
	# location resolution is needed here — the docname IS the key.
	reis = spray_rei.active_for_blocks([candidate], company)
	entity["active_reis"] = reis
	entity["active_rei_count"] = len(reis)
	entity["rei_blocked"] = bool(reis)

	return {
		"entity_type": BLOCK,
		"entity": entity,
		"docname": candidate,
		"alert_doctype": FIELD,
		"task_filters": [{"location_doctype": FIELD, "location": candidate}],
		"scan_recorded": False,
		"status": {
			"status": None,
			"needs_attention": bool(reis),
			"warnings": [{"kind": "rei", "severity": "Critical", "message": rei["warning"]} for rei in reis],
		},
	}


_CASCADE = (_employee_branch, _asset_branch, _housing_branch, _field_branch)


# ── universal_scan ──────────────────────────────────────────────────────────
def universal_scan(args: dict) -> ToolResult:
	"""Resolve any scanned string to whatever it identifies, with what is due on it."""
	raw = _content(args)
	_refuse_credential_payload(raw.strip())
	candidate = scan_target(raw)
	today = _today()
	limit = max(1, min(HISTORY_MAX, as_int(args, "history_limit", HISTORY_LIMIT)))

	resolved = None
	for probe in _CASCADE:
		resolved = probe(candidate, args)
		if resolved is not None:
			break

	if resolved is None:
		return _unresolved(raw, candidate, args)

	entity_type = resolved["entity_type"]
	docname = resolved["docname"]

	tasks = _tasks_linked_to(resolved["task_filters"])
	due_dates = _alert_due_dates(tasks)
	pending = [_describe_task(row, due_dates, today) for row in tasks]
	overdue = [task for task in pending if task["overdue"]]
	compliance = _alerts_for(resolved["alert_doctype"], docname, today) if docname else []

	data = {
		"content": raw,
		"resolved_from": candidate,
		"entity_type": entity_type,
		"entity": resolved["entity"],
		"entity_name": docname or None,
		"pending_tasks": pending,
		"pending_task_count": len(pending),
		"overdue_tasks": overdue,
		"overdue_task_count": len(overdue),
		"due_compliance": compliance,
		"due_compliance_count": len(compliance),
		"recent_history": _history(entity_type, docname, limit) if docname else [],
		"available_actions": list(AVAILABLE_ACTIONS[entity_type]),
		"scan_recorded": resolved["scan_recorded"],
		# Present on every branch, populated on the two that have a state machine
		# behind them. A client that renders the same card for five entity types
		# reads three keys that are always there rather than testing for them.
		**resolved.get("state", _EMPTY_STATE),
		# v0.78.0, same promise: the status panel and the warning banner are on
		# every branch and populated on the two that have anything to say.
		**resolved.get("status", _EMPTY_STATUS),
	}
	return ToolResult(
		data=data,
		summary=_summary(entity_type, docname, resolved["entity"], pending, overdue, compliance),
		docstatus_delta="0 → 0 (updated)" if resolved["scan_recorded"] else "",
	)


def _unresolved(raw: str, candidate: str, args: dict) -> ToolResult:
	"""The scan that is not in any register. A shape, not a refusal.

	Every key the resolved branches return is present and empty, so a client
	renders one screen rather than branching on which keys arrived. `searched` is
	the honest part: it names the registers that were actually consulted, so
	"unknown" on a site whose Asset Register has not migrated does not read as
	"this tag is not an asset".

	THE NOTE QUOTES THROUGH `bucket_bridge._display` AND `content` DOES NOT. The
	two are different jobs on the same string. `content` is the SCAN, returned
	whole because the only useful thing to do with an unresolved tag is raise a
	task carrying it, and a truncated string is not a tag anybody can look up
	later. The note is a SENTENCE that will be drawn on a screen and read aloud
	across a yard, and it goes through the same twelve-character bound the badge
	step uses — enough to recognise which thing was scanned, not enough to be a
	credential. A credential-shaped payload never reaches either: `universal_scan`
	refuses it before any register is read.
	"""
	searched = [
		name
		for name, doctype in (
			(EMPLOYEE, BADGE_MAP),
			(ASSET, ASSET_REGISTER),
			(HOUSING, HOUSING_UNIT),
			(BLOCK, FIELD),
		)
		if compat.doctype_exists(doctype)
	]
	return ToolResult(
		data={
			"content": raw,
			"resolved_from": candidate,
			"entity_type": UNKNOWN,
			"entity": None,
			"entity_name": None,
			"pending_tasks": [],
			"pending_task_count": 0,
			"overdue_tasks": [],
			"overdue_task_count": 0,
			"due_compliance": [],
			"due_compliance_count": 0,
			"recent_history": [],
			"available_actions": list(AVAILABLE_ACTIONS[UNKNOWN]),
			"scan_recorded": False,
			**_EMPTY_STATE,
			**_EMPTY_STATUS,
			"searched": searched,
			"note": (
				"Nothing in this site's badge, asset, housing or block register is called "
				f"{bucket_bridge._display(candidate)}. The scan is reported rather than refused so "
				"a task can be raised about whatever it was — `content` carries it whole."
			),
		},
		summary=f"unresolved scan ({len(candidate)} characters), searched {len(searched)} register(s)",
	)


def _summary(entity_type: str, docname: str, entity: dict, pending: list, overdue: list, due: list) -> str:
	"""One sentence: what it was, and what is outstanding on it."""
	if entity_type == EMPLOYEE:
		headline = f"badge → {entity.get('employee_name') or docname}"
		if entity.get("on_active_shift"):
			headline += f", on shift {entity['active_shift']['shift']}"
	elif entity_type == ASSET:
		headline = f"asset {docname} ({entity.get('asset_type') or 'untyped'}), scan recorded"
	elif entity_type == HOUSING:
		headline = f"housing unit {docname} ({entity.get('currently_assigned') or 0} assigned)"
	else:
		headline = f"block {docname} ({entity.get('zone_count') or 0} zone(s))"

	outstanding = []
	if pending:
		outstanding.append(f"{len(pending)} open task(s)")
	if overdue:
		outstanding.append(f"{len(overdue)} overdue")
	if due:
		outstanding.append(f"{len(due)} compliance item(s)")
	return headline + (", " + ", ".join(outstanding) if outstanding else ", nothing outstanding")

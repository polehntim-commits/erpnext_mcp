# SPDX-License-Identifier: MIT
"""Turning what the tools return into what `FarmOpsKit` decodes.

THE TRANSLATION LIVES HERE AND NOT IN EITHER SIDE, and that is the whole point
of the file. The MCP tools answer an operator's question ("describe this task
for an audit record"); the app asks a worker's ("what am I holding, and what
does it still need"). Both shapes are right for their reader, so v0.17.1 adds a
translator rather than bending one reader's payload towards the other.

WHAT THE APP DECODES IS THE SPEC. `fafo_ios/API_CONTRACT.md` and the structs in
`FarmOpsKit/Sources/FarmOpsKit/Models/` are the contract, and where Wave A's
payload and Wave B's decoder disagreed the BACKEND moved — every gap below is
closed by emitting the field the app already expects, because the alternative is
shipping a new build to every phone in the valley to rename a key.

THE APP IS FORGIVING AND THIS STILL MATTERS. `API_CONTRACT.md` §"Client
tolerance" says booleans may be `1`/`"1"`/`true`, numbers may be strings, lists
may be bare or wrapped, and unknown enum values degrade to "unknown" rather than
failing the row. So a mismatch here is usually a blank field on a screen rather
than a crash — which is worse, not better: an inspection whose "Why this task
exists" card is silently missing looks like an inspection nobody could justify.

THE FIVE GAPS THIS FILE CLOSES, all of them found by reading the Swift:

  * `location_type`. The doctype field is `location_doctype`; the app decodes
    `location_type`. BOTH are emitted — the app gets its key, and an MCP client
    reading the same payload keeps the name the doctype uses.
  * `latitude` / `longitude`. No Farm Task carries coordinates. `Field` and
    `Irrigation Zone` carry a boundary centroid, so a task located on one gets a
    real pin; a Housing Unit has none and the keys are OMITTED rather than
    zeroed. `0, 0` is a real place in the Gulf of Guinea and a map that puts a
    cabin there is worse than a map with no pin.
  * `source_alert_explanation`. The app renders it verbatim under "Why this task
    exists" and HIDES THE CARD if it is absent — its contract says an inspection
    with no stated reason is worse than none. Only the server knows which rule
    fired, so it comes from the Compliance Alert's own message.
  * `assignment` / `claimed_at` / `started_at` on the TASK. They live on the
    Farm Task Assignment; the app's `FarmTask` carries them because a phone
    renders one card, not a join.
  * `companies` as objects, `default_company`, `roles`, `skills` on the user
    context. `get_current_user_context` answers with `entity_access` (strings),
    `preferred_company` and `mobile_roles`; the app decodes `companies`
    (`{name, abbr}`), `default_company`, `roles` and `skills`.
"""

from __future__ import annotations

import frappe

from .. import compat, locations, task_templates, timezones
from .. import roles as role_lib
from ..args import select_options
from . import rectify

ALERT = "Compliance Alert"
FARM_TASK = "Farm Task"
EMPLOYEE = "Employee"

#: Where a site might keep a worker's skills. None is created by this app — see
#: `tools/fieldwork.py`, which argues at length against inventing a register.
#: The app's `UserContext.skills` is a display field, so an empty list is a
#: truthful answer and a guessed one would not be.
_SKILL_FIELDS = ("farm_skills", "skills", "custom_farm_skills")

#: Doctypes that can answer "where is this, in degrees". A boundary centroid is
#: not the cabin door, but it is the right block, which is what a worker driving
#: to it needs.
_CENTROID = ("boundary_centroid_lat", "boundary_centroid_lon")

#: Compliance Alert severity → the app's `TaskUrgency`. The app has four levels
#: and the alert register has three, so Warning maps to High: an overdue
#: habitability inspection is not a "normal" item on a phone, and rounding it
#: down would sort it below routine work.
_SEVERITY_TO_URGENCY = {"Critical": "Critical", "Warning": "High", "Info": "Normal"}


# ── tasks ───────────────────────────────────────────────────────────────────
def _first(live: dict, row: dict, key: str):
	"""The assignment's value for `key`, or the task row's, or None.

	Presence rather than truthiness on the FIRST source only: an assignment that
	carries the key with an empty value has genuinely not got one, and falling
	through to the task row is right; an assignment that answers 0 has answered.
	"""
	value = live.get(key)
	if value not in (None, ""):
		return value
	value = row.get(key)
	return value if value not in (None, "") else None


def _minutes_since(stamp):
	"""Whole minutes between a stored timestamp and now, or None.

	Used for the paused counter, where "twenty-two minutes ago" is the whole
	value of the field: a worker who is told a task is paused shrugs, and a
	worker who is told they paused it twenty-two minutes ago turns round.

	Never raises. A stamp nothing can parse comes back as None, which the client
	renders as "paused" without the number — losing a reminder over a clock is
	the wrong trade.
	"""
	if not stamp:
		return None
	try:
		return max(0, round(float(frappe.utils.time_diff_in_seconds(frappe.utils.now(), stamp)) / 60.0))
	except Exception:  # pragma: no cover - an unparseable stored timestamp
		return None


def _minutes(value):
	"""A duration as a whole number of minutes, or None.

	Zero survives as zero. A task completed inside the same minute it started is
	a real completion with a real duration, and folding it to None would put the
	handset back on the counting-from-`startedAt` path this field exists to stop.
	"""
	if value in (None, ""):
		return None
	try:
		return int(float(value))
	except (TypeError, ValueError):
		return None


def task(row: dict, assignment: dict | None = None, clock=None) -> dict:
	"""One Farm Task in the shape `FarmOpsKit.FarmTask` decodes.

	`clock` is a `timezones.Renderer`, threaded in by a caller shaping more than
	one task so the site's zone is read once for the response rather than once
	per row. Omitted, one is made here — a single-task read should not have to
	construct furniture to get a timestamp it can act on.
	"""
	row = dict(row or {})
	live = dict(assignment or {})
	out = {
		"name": row.get("name"),
		"task_name": row.get("task_name") or row.get("name"),
		"task_type": row.get("task_type") or "Other",
		"state": row.get("state") or "Draft",
		"urgency": row.get("urgency") or "Normal",
		"dispatch_mode": row.get("dispatch_mode") or "Either",
		"estimated_duration_minutes": row.get("estimated_duration_minutes"),
		"skill_required": row.get("skill_required"),
		"notes": row.get("notes"),
		"company": row.get("company"),
		"location": row.get("location"),
		# Both spellings, deliberately. See the module docstring.
		"location_type": row.get("location_doctype") or row.get("location_type"),
		"location_doctype": row.get("location_doctype"),
		"evidence_required": row.get("evidence_required") or {},
		"creates_record": row.get("creates_record"),
		"produced_record": row.get("produced_record"),
		"source_alert": row.get("source_alert"),
		"assignment": live.get("name") or row.get("assignment"),
		"assigned_to": live.get("assigned_to") or row.get("assigned_to"),
		"assigned_to_name": live.get("assigned_to_name") or row.get("assigned_to_name"),
		"claimed_at": live.get("claimed_at"),
		"started_at": live.get("started_at"),
		# v0.76.0. THE FINISHING TIMESTAMPS, WHICH THE APP DECODES AND THE SERVER
		# WAS NOT SENDING. `complete_task_via_mobile` writes both onto the Farm
		# Task Assignment and `_describe_assignment` has reported them since
		# v0.16.0, but `shape.task` never carried them up onto the task — so
		# `FarmTask.completedAt` and `.actualDurationMinutes` decoded as nil on
		# every read, and `FarmTask.elapsedMinutes` fell through to counting from
		# `startedAt` to NOW. A finished task re-opened the next morning showed a
		# timer that had been running all night. The doctype had the answer the
		# whole time; this is the line that returns it.
		#
		# Read from the assignment FIRST and the task row second: the assignment
		# is where a completion is recorded, and the task row carries the same two
		# columns for the tools that describe a task on its own.
		"completed_at": _first(live, row, "completed_at"),
		"actual_duration_minutes": _minutes(_first(live, row, "actual_duration_minutes")),
	}

	# v0.79.0. WHICH TASKS ARE PAUSED, on every row that carries a state. A board
	# that showed "In-Progress" against three tasks would be a board a worker
	# cannot read, because they are on one of them — `state` already says Paused,
	# and these three keys are what let a client sort, badge and count without
	# string-matching a state name it would have to keep in step with the server.
	#
	# `paused_minutes_ago` is the one that changes behaviour. "You have a paused
	# task" is a notification; "you paused Irrigate Block 3 twenty-two minutes
	# ago" is a worker turning round.
	out["paused"] = str(out["state"]) == "Paused"
	out["paused_at"] = _first(live, row, "paused_at")
	out["pause_reason"] = _first(live, row, "pause_reason")
	out["pause_count"] = int(_first(live, row, "pause_count") or 0)
	out["auto_paused"] = bool(_first(live, row, "auto_paused"))
	out["paused_minutes_ago"] = _minutes_since(out["paused_at"]) if out["paused"] else None
	# The step counters, so a handset can draw "3 of 5 done" on an investigation
	# without a second call per row.
	out["parent_task"] = row.get("parent_task") or None
	out["merged_into"] = row.get("merged_into") or None
	# v0.98.0, item 5. WHEN IT WAS SEEN, and this shaper is exactly why it needed
	# saying: `dispatch._describe_task` reports the column and this function
	# rebuilds its payload key by key, so a fact the tool knows is dropped on the
	# way to the handset unless it is named here. That is the same mechanism that
	# lost `template` until v0.96.0 — see the block below — and the app posting
	# `observed_at` and never reading it back is how a client author concludes
	# the server ignored it. Present only where somebody said so.
	if row.get("observed_at"):
		out["observed_at"] = str(row["observed_at"])

	# v0.96.0. THE TEMPLATE A TASK CAME FROM, AND THE CHECKLIST IT SNAPSHOTTED.
	# `dispatch._describe_task` has reported both since v0.41.0 and this shaper
	# rebuilds its payload key by key, so both were dropped on the way to the
	# handset — `get_task` answered with thirty-two fields and none of them named
	# the template. The consequence is not cosmetic: a worker holding a task had
	# no way to reach the template's SOP, its instructions or its checklist,
	# because there was nothing in the answer saying which template to ask for.
	#
	# PRESENT ONLY WHERE THERE IS ONE, which is the rule `_describe_task` already
	# follows: most tasks are raised by hand and have neither, and a payload that
	# grew two permanent nulls for them would be a change to every row to serve
	# the rows that came off a template.
	if row.get("template"):
		out["template"] = row["template"]
		# v0.98.0. THE PROCEDURE ITSELF, NOT ONLY THE NAME OF THE RECORD THAT
		# HOLDS IT. v0.96.0 put the template's docname in the answer, which told
		# the app which record to ask for and left it a second round trip away
		# from the document — on a tethered handset at a cabin door, with a
		# picker waiting. These two are read THROUGH the link rather than copied
		# onto the task, which is the whole reason `task_templates.snapshot` does
		# not carry them: an SOP replaced at the office reaches every open task,
		# where a snapshotted one would leave last season's PDF attached to work
		# still on the board.
		#
		# ABSENT WHERE THERE IS NO DOCUMENT, like `template` itself. A key that
		# was always there and usually null would put two permanent nulls on
		# every templated row to serve the ones with a procedure filed.
		for field, url in task_templates.sop_documents(row["template"]).items():
			if url:
				out[f"sop_document_{field}"] = url
	if row.get("checklist"):
		out["checklist"] = row["checklist"]
		out["checklist_done"] = row.get("checklist_done")
		out["checklist_outstanding_required"] = row.get("checklist_outstanding_required")

	# v0.77.0. WHICH six o'clock a job was claimed, started and finished. The
	# three existing keys are untouched and still naive — `FrappeDate.parse` on
	# the handset reads them and would fail the whole row on a shape it has not
	# seen — and each gains a `*_local` twin carrying the offset. A worker looking
	# at "finished 16:04" should not have to know whether the server meant their
	# afternoon or somebody else's. See `erpnext_mcp/timezones.py`.
	#
	# THE ZONE BLOCK IS NOT ON THE ROW. It is one fact about the response, and
	# repeating three keys on each of forty tasks is forty copies of it — the
	# caller puts `clock.block()` at the top level once. `tasks()` below threads
	# one renderer through the whole list so the site's zone is read once too.
	(clock or timezones.Renderer()).add(out, "claimed_at", "started_at", "completed_at", "paused_at")

	source = alert_row(row.get("source_alert"))
	explanation = str(source.get("alert_message") or "").strip()
	if explanation:
		out["source_alert_explanation"] = explanation
	# v0.57.0, and additive: a signature task now carries the ADDRESS of the box
	# it is asking for, per `API_CONTRACT.md` §14.1. The task NAME is a sentence
	# — "Collect I-9 Section 2 signature for Juan Lopez" — and a sentence is not
	# something a handset can post a signature against; the docname is not in it,
	# and deriving one from the employee's name is how ink lands on the wrong
	# person's form. It comes off the alert the sweep raised the task from, which
	# is the same source the compliance calendar reads, so the pad opened from
	# the Available tab and the pad opened from the calendar are addressed
	# identically. A task with no alert behind it is ordinary work and gets none.
	request = signature_request(source)
	if request:
		out["signature_request"] = request

	# v0.176.0. THE CLASS, WHERE THIS TASK IS ONE. A Training task is the whole
	# of what a worker deals with — it is on their Today screen, it holds the
	# paperwork, it is what they finish — so the day, the venue, the provider and
	# the head count ride on the task rather than behind a second fetch of a
	# doctype the handset would otherwise have to know about. Read off the
	# session by `dispatch._training_details`; stored nowhere.
	#
	# PRESENT ONLY WHERE THERE IS ONE, the same rule as `signature_request` above
	# and `subject_doctype` below: every other task's payload is untouched.
	if isinstance(row.get("training"), dict) and row["training"].get("session"):
		out["training"] = row["training"]
	# The reference itself, for a client that wants the docname rather than the
	# details — the documents card asks the server for this session's folder.
	if row.get("subject_doctype"):
		out["subject_doctype"] = row.get("subject_doctype")
		out["subject_docname"] = row.get("subject_docname")

	latitude, longitude = coordinates(row.get("location_doctype"), row.get("location"))
	if latitude is not None and longitude is not None:
		out["latitude"] = latitude
		out["longitude"] = longitude
	return out


def tasks(rows: list, assignments: dict | None = None, clock=None) -> list:
	"""A list of tasks, each carrying its live assignment where there is one."""
	by_task = assignments or {}
	clock = clock or timezones.Renderer()
	return [
		task(row, by_task.get(str(row.get("name"))), clock) for row in rows or [] if isinstance(row, dict)
	]


def coordinates(location_doctype, location) -> tuple:
	"""Degrees for a task's location, or (None, None) where the site has none.

	Never raises and never guesses. A doctype without a centroid, a document that
	has been deleted, or a boundary nobody has drawn all give the same answer,
	and the caller omits the keys rather than sending a placeholder.
	"""
	doctype = str(location_doctype or "").strip()
	name = str(location or "").strip()
	if not doctype or not name:
		return None, None
	try:
		if not compat.has_field(doctype, _CENTROID[0]):
			return None, None
		row = frappe.db.get_value(doctype, name, list(_CENTROID), as_dict=True) or {}
	except Exception:  # pragma: no cover - a doctype this site cannot read
		return None, None
	try:
		latitude = float(row.get(_CENTROID[0]))
		longitude = float(row.get(_CENTROID[1]))
	except (TypeError, ValueError):
		return None, None
	if latitude == 0 and longitude == 0:
		# Null Island. A boundary that centroids to exactly zero is an empty
		# field, not a farm in the Atlantic.
		return None, None
	return latitude, longitude


#: What a task needs to know about the alert behind it: the sentence the rule
#: wrote, and enough of the alert's identity to work out whether it is asking for
#: a signature. ONE READ RATHER THAN TWO — a task list is one of these per row.
_ALERT_FIELDS = ("name", "alert_type", "alert_message", "source_doctype", "source_docname", "due_date")


def alert_row(alert) -> dict:
	"""The Compliance Alert behind a task, or `{}`. Never raises."""
	name = str(alert or "").strip()
	if not name or not compat.doctype_exists(ALERT):
		return {}
	try:
		return dict(
			frappe.db.get_value(ALERT, name, compat.existing_fields(ALERT, _ALERT_FIELDS), as_dict=True) or {}
		)
	except Exception:  # pragma: no cover
		return {}


def alert_explanation(alert) -> str:
	"""Why this task exists, in the words the rule that raised it used.

	The app renders this VERBATIM and hides its card without it, so this returns
	"" rather than a composed sentence when there is no alert behind the task —
	most work is just work, and "this task exists because somebody made it" is
	not an explanation worth a card.
	"""
	return str(alert_row(alert).get("alert_message") or "").strip()


def signature_request(row: dict) -> dict | None:
	"""The §14.1 address an alert row is asking for, or None. Never raises.

	Delegated to `tools/signatures.py` rather than composed here, because that
	module holds the closed table of boxes this app will write a signature into
	— and a request the calendar composed independently could address a column
	the write path refuses, which is a pad that collects ink into nothing.
	"""
	if not row:
		return None
	try:
		from ..tools import signatures

		return signatures.request_for_alert(row)
	except Exception:  # pragma: no cover - a site whose form doctypes are absent
		return None


# ── the compliance calendar ─────────────────────────────────────────────────
def alert(row: dict) -> dict:
	"""One calendar row in the shape `ComplianceAlertSummary` decodes.

	v0.57.0 ADDS THREE KEYS AND CHANGES NONE, per `API_CONTRACT.md` §8.1. An
	alert sent exactly as v0.55.0 sent it still lists, still sorts and still
	opens its detail — every one of these is optional on the phone, and an alert
	carrying none of them is the noticeboard row it always was.

	  * `signature_request` — the blank box this alert is about, addressed. The
	    row becomes a tap that opens a signature pad instead of a sentence
	    somebody has to go and find the Farm Task for. Composed by
	    `tools/signatures.py` off the table the WRITE path gates on, so a pad can
	    only ever be opened at a column this app would accept ink into.
	  * `subject_doctype` / `subject_docname` — the record the alert concerns,
	    which the alert has carried since it was raised. Displayed rather than
	    navigated: the app has no screen for an arbitrary doctype and does not
	    pretend to.
	  * `can_dismiss` — whether THIS alert may be closed without the work behind
	    it being done. False unless somebody said otherwise, which is why the
	    handset's Dismiss button is absent by default.

	`signature_request` IS NESTED RATHER THAN SPREAD FLAT. The app reads both
	spellings and the nested one is the contract's own; flattening would put
	`signature_field` at the top level of a row that also carries a `name`, which
	is one key collision away from a pad addressed at the alert instead of at the
	form.


	v0.106.0 ADDS `subject_employee` AND `subject_employee_name`, AND THEY EXIST
	TO STOP A CLIENT READING A NAME OUT OF PROSE. Until this release the only
	person on an alert was inside `explanation` — *"Applicator License — Timothy
	Polehn 2025 EXPIRED 36 day(s) ago"* — and the app was matching candidate names
	against that sentence to keep somebody from being handed the task of signing
	off their own gap. That is a whole-word string search standing in for a
	foreign key, and it fails in both directions: a worker whose name is spelled
	differently on the certificate is not excluded, and an alert that happens to
	quote a second person excludes them too.

	`subject_employee` is the Employee docname the sweep derived from the SOURCE
	RECORD (`alerts/base.py::subject_employee`), and `subject_employee_name` is
	that person's display name so a screen showing one does not need a second
	call. Both are `None` far more often than not, and `None` MEANS "this alert
	is about the operation, or about nobody this server could name" — never
	"look in the prose instead".

	SPRINT 3 (v0.68.0) ADDS A FOURTH, `rectification` — see `api/rectify.py`. It
	answers "what fixes this, and what do I call to start it" for every alert
	type this release names one for: `action_type`, `action_label`,
	`action_endpoint` (a sidecar route, absolute from `/farmops/api/mobile/`),
	`action_params` (what to prefill) and `can_rectify_mobile`. Omitted only
	where `describe_rectification` itself returns nothing, which it does not for
	a row it can read — the "nothing this app can do from a phone yet" case is
	still a `rectification` object, with `can_rectify_mobile: false` and an
	`explanation`, so the app can tell "no fix" from "row not decoded".
	"""
	row = dict(row or {})
	out = {
		"name": row.get("name"),
		"title": row.get("title") or row.get("alert_type") or row.get("name"),
		"explanation": row.get("message") or row.get("alert_message"),
		"due_date": row.get("due_date"),
		"urgency": _SEVERITY_TO_URGENCY.get(str(row.get("severity") or ""), "Normal"),
		"severity": row.get("severity"),
		"company": row.get("company"),
		"regulation": row.get("framework"),
		"linked_task": linked_task(row.get("name")),
		"overdue": row.get("overdue"),
		"days_until_due": row.get("days_until_due"),
		"subject_doctype": row.get("source_doctype"),
		"subject_docname": row.get("source_docname"),
		# v0.106.0. WHO THE ALERT IS ABOUT, AS A DOCNAME. See the docstring.
		"subject_employee": row.get("subject_employee") or None,
		"subject_employee_name": row.get("subject_employee_name") or None,
		"can_dismiss": bool(row.get("can_dismiss")),
	}
	request = row.get("signature_request")
	if request:
		out["signature_request"] = request
	rectification = rectify.describe_rectification(row)
	if rectification:
		out["rectification"] = rectification
	return out


def alerts(rows: list) -> list:
	return [alert(row) for row in rows or [] if isinstance(row, dict)]


def linked_task(alert_name) -> str | None:
	"""The Farm Task raised from this alert, so the calendar can say "task raised".

	Newest first: an alert that was answered, rejected and re-raised has several,
	and the one a worker cares about is the live one.
	"""
	name = str(alert_name or "").strip()
	if not name or not compat.doctype_exists(FARM_TASK):
		return None
	try:
		rows = frappe.db.get_all(
			FARM_TASK,
			filters={"source_alert": name},
			pluck="name",
			order_by="creation desc",
			limit=1,
		)
	except Exception:  # pragma: no cover
		return None
	return rows[0] if rows else None


# ── the user context ────────────────────────────────────────────────────────
def user_context(data: dict, user: str) -> dict:
	"""`get_current_user_context` in the shape `UserContext` decodes.

	`roles` is EVERY role the account holds, not just this app's six. The app
	intersects it with {Compliance Officer, Foreman, Farm Manager, System
	Manager} to decide whether to draw the Compliance tab — and its own contract
	is explicit that this is UI courtesy and not the security boundary, which is
	why `list_compliance_alerts` gates independently. It is the caller's own
	role list either way.
	"""
	data = dict(data or {})
	companies = [name for name in (data.get("entity_access") or []) if name]
	return {
		"user": data.get("user") or user,
		"full_name": data.get("full_name") or user,
		"employee": data.get("employee"),
		"roles": data.get("all_roles") or role_lib.all_roles_of(user),
		"mobile_roles": data.get("mobile_roles") or [],
		# v0.108.0, S11. THE BADGE, WORKED OUT ON THE SERVER. `roles` above is
		# every role the account holds and the app was intersecting it with a
		# hardcoded Swift list to decide what one word to draw next to somebody's
		# name — a copy of this app's role vocabulary compiled into a binary,
		# stale the release a role is added or renamed. `roles.role_indicator`
		# picks it here, from the table that defines the roles, and states its own
		# precedence rule. See `roles.ROLE_INDICATORS`; it is a DISPLAY fact and
		# not a permission, and `can_dispatch` inside it is the same courtesy
		# `capability_of` documents at length.
		"role_indicator": role_lib.role_indicator(data.get("user") or user),
		"companies": [{"name": name, "abbr": company_abbr(name)} for name in companies],
		"default_company": data.get("preferred_company") or (companies[0] if companies else None),
		"skills": skills_of(data.get("employee")),
		"enabled": data.get("enabled"),
		"grant_state": data.get("grant_state"),
		# The credential's review date is NOT sent. `UserContext` does not decode
		# it, `guard.strip_secrets` would take half of it anyway on the name, and
		# a phone that cannot act on a fact is a phone that should not be told it
		# — the operator reads it in `list_mobile_users`, which is where the
		# person who can re-issue a token is looking.
		#
		# v0.98.0, item 5. The vocabularies this site will accept, read off its
		# own meta at the one call every handset makes at login. See `taxonomy`.
		"taxonomy": taxonomy(),
	}


#: The Select fields whose options a handset needs before it can offer a picker,
#: as `{key in the answer: (doctype, fieldname)}`.
#:
#: v0.98.0, ITEM 5. THE APP HAD TRANSCRIBED THE FIRST OF THESE INTO SWIFT.
#: `FarmTaskType.all` is a hand-copied list of the eleven options
#: `Farm Task.task_type` shipped with, and the server's own refusal names them
#: — "task_type must be one of: Inspection, Test, Spray, …" — which is what let
#: the app offer a grid instead of a text box in the first place. The cost of
#: the copy is that a site which CUSTOMISES the Select, which is a supported
#: thing to do and needs no code release on this side, gets a picker missing its
#: own options until the app ships again. Reading it beats transcribing it.
#:
#: FOLDED INTO `get_current_user_context` RATHER THAN GIVEN A ROUTE, which is
#: what the plan allowed and is the cheaper half of it: this is the call the app
#: already makes at login and on every manual refresh, so the taxonomy arrives
#: exactly as often as it can change and costs no extra request on a tethered
#: connection in an orchard.
#:
#: THE LOCATION REGISTERS ARE HERE TOO AND ARE NOT A SELECT. `location_doctype`
#: is a Link to DocType and its four legal values live in a sentence inside
#: three separate tool refusals; `TaskLocationRegister` transcribed them from
#: that sentence. `list_farm_locations` answers with rows FROM those registers,
#: and this says what the registers are — which is what a picker needs to draw
#: its four sections before any of them has a row in it.
TAXONOMY_SELECTS = {
	"task_types": ("Farm Task", "task_type"),
	"task_urgencies": ("Farm Task", "urgency"),
	"dispatch_modes": ("Farm Task", "dispatch_mode"),
	"break_kinds": ("Farm Shift Compliance Event", "break_kind"),
	"report_directions": ("Farm Incident Record", "report_direction"),
	"discipline_types": ("Farm Incident Record", "discipline_type"),
}


def taxonomy() -> dict:
	"""What this site's Select fields actually offer, so nothing is transcribed.

	READ OFF META, NEVER HARDCODED, which is the whole point — a constant here
	would be the same copy `FarmTaskType.all` already is, one layer further from
	the client and just as stale. `args.select_options` returns the options in
	the doctype's own casing and drops the leading blank, so what comes back is
	exactly what `as_choice` will match against.

	A DOCTYPE THIS SITE HAS NOT MIGRATED CONTRIBUTES AN EMPTY LIST rather than
	taking the login call down with it. An empty list is honest — the app falls
	back to what it has and the operator can see which register is missing — and
	`get_current_user_context` doubles as credential validation, so a failure
	here would read on the handset as "this token is dead, sign out".
	"""
	out = {}
	for key, (doctype, fieldname) in TAXONOMY_SELECTS.items():
		try:
			out[key] = select_options(doctype, fieldname) if compat.doctype_exists(doctype) else []
		except Exception:  # pragma: no cover - a half-migrated site, not one of ours
			out[key] = []
	out["location_registers"] = [
		register for register in locations.REGISTERS if compat.doctype_exists(register)
	]
	return out


def company_abbr(name) -> str | None:
	try:
		return str(frappe.db.get_value("Company", name, "abbr") or "") or None
	except Exception:  # pragma: no cover
		return None


def skills_of(employee) -> list:
	"""What this site records about a worker's skills. Usually nothing.

	Returns [] rather than inventing a register or reading a job title — see
	`tools/fieldwork._skill_for`, which makes the argument. The app shows the
	list and filters nothing by it, so an empty one costs a line on a settings
	screen and a guessed one would hide work.
	"""
	name = str(employee or "").strip()
	if not name or not compat.doctype_exists(EMPLOYEE):
		return []
	field = compat.first_field(EMPLOYEE, *_SKILL_FIELDS)
	if not field:
		return []
	try:
		raw = str(frappe.db.get_value(EMPLOYEE, name, field) or "")
	except Exception:  # pragma: no cover
		return []
	return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]


# ── completion ──────────────────────────────────────────────────────────────
def completion(data: dict) -> dict:
	"""`complete_farm_task`'s result in the shape `CompletionResult` decodes.

	`created_record_name` is what closes the loop on the phone — the worker reads
	"Housing Inspection HI-2026-00042 created · compliance alert cleared" instead
	of "Done", and the app's contract asks for it by name for exactly that reason.

	`dismissed_alert` IS NOT A CLAIM THAT AN ALERT WAS DISMISSED. Nothing in this
	app dismisses an alert directly. The record moves the register forward and a
	SWEEP notices the condition is no longer true — `complete_farm_task` argues
	that this is the only honest way for an alert to go away. What this field
	names is the alert the work ANSWERED, which is what the phone means when it
	says "cleared", and it is only populated when the completion actually produced
	the record that answers it.

	v0.64.0 MOVED WHEN THAT SWEEP RUNS AND NOT WHAT THIS FIELD MEANS. The
	completion now re-runs the rule that raised the task, and the rules that read
	the register it wrote to, at the moment it files — so the alert usually is
	gone by the time the phone reads this. It is still the rule that decided, and
	this field still names what the work answered rather than what was cleared;
	a completion against a condition that is still true leaves the alert standing
	and this field populated, which is correct and is the case the distinction
	exists for.

	THE SERVER'S RESULT CARRIES MORE THAN THIS. `shift_evidence` and
	`compliance_evaluation` are on `complete_farm_task`'s own payload and are
	deliberately not projected here: this is a strict projection, the handset's
	`CompletionResult` decodes exactly these keys, and a field the app cannot yet
	render belongs on the MCP surface until the app asks for it.
	"""
	data = dict(data or {})
	task_row = dict(data.get("task") or {})
	produced = data.get("produced_record")
	answered = task_row.get("source_alert") if produced else None
	return {
		"task": task_row.get("name"),
		"created_record_doctype": data.get("produced_record_doctype"),
		"created_record_name": produced,
		"dismissed_alert": answered,
		"corrective_action_opened": data.get("produced_record_state") == "Corrective Action Required",
		"final_state": data.get("final_state"),
		"evidence_filed": data.get("evidence_filed"),
		"record_note": data.get("record_note"),
		# v0.20.1. `x_idempotent` IS THE FIELD THE APP'S SYNC QUEUE READS, and it
		# is always present so that reading it is one branch rather than two.
		# True means "this exact completion was already on record and nothing
		# changed" — which is a success, and the queue item may be cleared. A
		# genuinely conflicting resubmission never reaches here; it is still an
		# error, from complete_farm_task, unchanged.
		#
		# `visit_id` is echoed so the handset can prove the grouping survived the
		# round trip, and `completion_signature` so a client that wants to can
		# hold the server's own identifier for the submission it filed.
		"x_idempotent": bool(data.get("x_idempotent")),
		"idempotent_note": data.get("idempotent_note"),
		"visit_id": data.get("visit_id"),
		"completion_signature": data.get("completion_signature"),
		# v0.176.0. WHAT HAPPENED TO THE CLASS THIS TASK WAS RAISED FOR, and the
		# three keys are flat rather than a nested object because the projection
		# above is flat and the handset decodes every key leniently.
		#
		# PROJECTED RATHER THAN LEFT ON THE MCP SURFACE — which is the exception
		# the paragraph above describes, and it is earned. The worker who just
		# closed a training task is the person who needs to know whether the
		# afternoon FILED: "twelve records" and "nothing filed, nobody signed"
		# are the two outcomes, they are indistinguishable from "Done", and the
		# second one is fixable in the ten minutes while everybody is still in
		# the room. A note they read tomorrow is a note that costs a re-run.
		**_training_close(data),
	}


def _training_close(data: dict) -> dict:
	"""The training-session close-out, flattened. Absent where this task was not
	a class, so nothing about any other completion's payload changed."""
	close = data.get("training_session")
	if not isinstance(close, dict) or not close.get("training_session"):
		return {}
	return {
		"training_session": close.get("training_session"),
		"training_session_completed": bool(close.get("completed")),
		"training_session_note": close.get("note"),
	}

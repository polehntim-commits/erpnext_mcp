# SPDX-License-Identifier: MIT
"""Everything about the thing that was just scanned, on one screen.

THE PROBLEM IS THE NUMBER OF ROUND TRIPS, NOT THE ABSENCE OF DATA. A worker
walks up to a tractor and scans the tag. Every fact they need already exists
somewhere in this app — the register knows what it is, the state log knows who
has it, the engine-hours series knows what it has run, the schedule knows what
it is due, the task board knows what is outstanding on it, the alert register
knows what is late, and the REI register knows whether the block it is parked in
is closed to entry. Assembling that took seven calls, and a handset at the end of
a row on a rural cell makes about two before somebody puts the phone away.

So this is one function that composes the seven, and both scan routes call it.
`scan_asset` and `universal_scan` answering with different amounts of the same
picture is how a handset ends up with two code paths for one card — which is the
same argument v0.77.0 made when it put the action menu on both.

WHAT "COMPREHENSIVE" IS ALLOWED TO COST. Nothing in here writes, everything in
here is bounded, and every section degrades to an empty block with a reason
rather than raising. A scan is the single most latency-sensitive call this app
makes and it is made by somebody standing in the sun; a section whose doctype has
not migrated, whose column a site shapes differently, or whose register is empty
must lose THAT SECTION and not the scan. Every `except` below is that rule, and
none of them is a swallowed bug: the failure surfaces as a named absence in
`sections_unavailable`, which is a thing an operator can read.

WHY THE REI SECTION IS KEYED ON THE ASSET'S LOCATION. A restriction is a fact
about a BLOCK. A sprayer parked in the shed restricts nothing; a sprayer that
sprayed four blocks this morning left four windows, and the person scanning it
wants to know that before they drive it anywhere. So two questions are answered
and kept apart: `location_reis` is "the ground this machine is standing on is
closed" and `applied_reis` is "this machine is what closed those blocks". A
screen that merged them would tell a worker they may not enter the tractor shed.
"""

from __future__ import annotations

import frappe

from .. import compat
from . import asset_tags, engine_hours, maintenance, spray_rei

ASSET_REGISTER = asset_tags.ASSET_REGISTER
ASSET_STATE_LOG = asset_tags.ASSET_STATE_LOG
FARM_TASK = "Farm Task"
FARM_TASK_ASSIGNMENT = "Farm Task Assignment"
ALERT = "Compliance Alert"
IRRIGATION_ZONE = "Irrigation Zone"

#: How many state changes the activity log carries. Ten is a phone screen; the
#: eleventh is one nobody scrolls to, and `list_asset_state_history` is where a
#: reader who wants the whole timeline goes.
ACTIVITY_LIMIT = 10
ACTIVITY_MAX = 50

#: Most tasks or alerts one scan reports. Past this something is wrong with the
#: data rather than busy on the farm, and a tag in somebody's hand is not where
#: that should be discovered.
LINKED_CAP = 50

#: The runtime windows a valve's scan reports, as (key, days back). "Today" is
#: the shift somebody is on; "this week" is the set somebody is behind on; "this
#: season" is the figure a water right is measured against. All three come from
#: the same `_runs_for` pass the water report uses.
RUNTIME_WINDOWS = (("today", 0), ("week", 6), ("season", 364))

_LOG_FIELDS = (
	"name",
	"action",
	"from_state",
	"to_state",
	"performed_by",
	"performed_at",
	"notes",
	"cascaded_from",
	"engine_hours",
	"hours_used",
	"creation",
)

_TASK_FIELDS = (
	"name",
	"task_name",
	"task_type",
	"state",
	"urgency",
	"assigned_to",
	"assigned_to_name",
	"skill_required",
	"location_doctype",
	"location",
	"asset",
	"creation",
)

_ALERT_FIELDS = (
	"name",
	"alert_type",
	"severity",
	"category",
	"alert_message",
	"status",
	"due_date",
	"first_seen",
	"can_dismiss",
)


def _today() -> str:
	return str(frappe.utils.today())


def _now() -> str:
	return str(frappe.utils.now())


# ── the sections ────────────────────────────────────────────────────────────
def _activity(asset_name: str, limit: int) -> list[dict]:
	"""The last few state changes, newest first. The "who did what" panel."""
	if not compat.doctype_exists(ASSET_STATE_LOG):
		return []
	try:
		rows = (
			frappe.db.get_all(
				ASSET_STATE_LOG,
				filters={"asset_name": asset_name},
				fields=compat.existing_fields(ASSET_STATE_LOG, _LOG_FIELDS),
				order_by="creation desc",
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
				"log_name": row.get("name"),
				"action": row.get("action"),
				"from_state": row.get("from_state") or None,
				"to_state": row.get("to_state") or None,
				"performed_by": row.get("performed_by") or None,
				"performed_at": str(row.get("performed_at") or row.get("creation") or ""),
				"notes": row.get("notes") or None,
				# On every row rather than only the cascaded ones, for the reason
				# `list_asset_state_history` gives: a reader working out whether
				# somebody was actually here needs the answer on each row, not the
				# absence of a key on most of them.
				"cascaded": bool(row.get("cascaded_from")),
				"cascaded_from": row.get("cascaded_from") or None,
				"engine_hours": (
					round(float(row["engine_hours"]), 1) if row.get("engine_hours") is not None else None
				),
				"hours_used": round(float(row["hours_used"]), 1) if row.get("hours_used") else None,
			}
		)
	return out


def paused_tasks_for(worker: str, limit: int = LINKED_CAP) -> list[dict]:
	"""Every task this worker paused and has not come back to. Never raises.

	ON A SCAN BECAUSE OF WHERE THE INTERRUPTION HAPPENS. A worker sets an
	irrigation line, is called to a broken valve, fixes it, and scans the next
	thing — and that scan is the moment they have forgotten the line. The
	sentence they need is not on the screen of the job they walked away from; it
	is on the screen in front of them now.

	`paused_minutes_ago` is what makes it useful. "You have a paused task" is a
	notification; "you paused Irrigate Block 3 twenty-two minutes ago" is a
	worker turning round.
	"""
	from .. import compat as _compat

	worker = str(worker or "").strip()
	if not worker or not _compat.doctype_exists(FARM_TASK_ASSIGNMENT):
		return []
	try:
		rows = (
			frappe.db.get_all(
				FARM_TASK_ASSIGNMENT,
				filters={"assigned_to": worker, "state": "Paused"},
				fields=compat.existing_fields(
					FARM_TASK_ASSIGNMENT,
					(
						"name",
						"task",
						"task_name",
						"paused_at",
						"pause_reason",
						"pause_count",
						"auto_paused",
						"actual_duration_minutes",
					),
				),
				order_by="paused_at desc",
				limit=limit,
			)
			or []
		)
	except Exception:  # pragma: no cover - a site shaping these columns differently
		return []

	now = _now()
	out = []
	for raw in rows:
		row = dict(raw)
		paused_at = str(row.get("paused_at") or "")
		minutes = None
		if paused_at:
			try:
				minutes = round(float(frappe.utils.time_diff_in_seconds(now, paused_at)) / 60.0)
			except Exception:  # pragma: no cover - an unparseable stored timestamp
				minutes = None
		out.append(
			{
				"assignment": row.get("name"),
				"task": row.get("task"),
				"task_name": row.get("task_name") or row.get("task"),
				"paused_at": paused_at or None,
				"paused_minutes_ago": minutes,
				"pause_reason": row.get("pause_reason") or None,
				"pause_count": int(row.get("pause_count") or 0),
				"auto_paused": bool(frappe.utils.cint(row.get("auto_paused"))),
				"minutes_already_worked": int(row.get("actual_duration_minutes") or 0),
				"message": (
					f"You have a paused task: {row.get('task_name') or row.get('task')}"
					+ (f" (paused {minutes} min ago)" if minutes is not None else "")
					+ (
						" — it was paused for you when you started something else."
						if frappe.utils.cint(row.get("auto_paused"))
						else ""
					)
				),
				"message_key": "task.paused_reminder",
			}
		)
	return out


def _open_tasks(asset_name: str) -> list[dict]:
	"""Live Farm Tasks against this asset, through either column that reaches it.

	BOTH `asset` AND THE DYNAMIC `location` PAIR, deduplicated. A field report
	writes the first and a dispatched job writes the second, and a worker holding
	the tag wants both — this is the same two-filter walk `universal_scan` makes,
	and a scan that read one column would hide half the board.
	"""
	if not compat.doctype_exists(FARM_TASK):
		return []
	from ..erpnext_mcp.doctype.farm_task.farm_task import STATES, TERMINAL_STATES

	live = [state for state in STATES if state not in TERMINAL_STATES]
	fields = compat.existing_fields(FARM_TASK, _TASK_FIELDS)
	if not fields:
		return []

	seen: dict = {}
	for filters in (
		{"asset": asset_name},
		{"location_doctype": ASSET_REGISTER, "location": asset_name},
	):
		if any(not compat.has_field(FARM_TASK, column) for column in filters):
			continue
		try:
			rows = (
				frappe.db.get_all(
					FARM_TASK,
					filters={**filters, "state": ("in", live)},
					fields=fields,
					order_by="creation desc",
					limit=LINKED_CAP,
				)
				or []
			)
		except Exception:  # pragma: no cover
			continue
		for raw in rows:
			row = dict(raw)
			seen.setdefault(
				str(row.get("name")),
				{
					"name": row.get("name"),
					"task_name": row.get("task_name") or None,
					"task_type": row.get("task_type") or None,
					"state": row.get("state") or None,
					"urgency": row.get("urgency") or "Normal",
					"assigned_to": row.get("assigned_to") or None,
					"assigned_to_name": row.get("assigned_to_name") or row.get("assigned_to") or None,
					"skill_required": row.get("skill_required") or None,
					"created_at": str(row.get("creation") or ""),
				},
			)
	return sorted(seen.values(), key=lambda row: row["created_at"], reverse=True)


def _alerts(asset_name: str, today: str) -> list[dict]:
	"""Open compliance alerts raised against this asset.

	KEYED ON `asset_register`, which is the column `scan_asset` has always read
	and the one the rule engine writes for an asset-shaped finding. Alerts that
	name the asset through `source_doctype`/`source_docname` are picked up too,
	because the rule engine writes THAT pair for most rules and an alert about
	this machine is an alert about this machine whichever column carries it.
	"""
	if not compat.doctype_exists(ALERT):
		return []
	fields = compat.existing_fields(ALERT, _ALERT_FIELDS)
	if not fields:
		return []

	filter_sets = []
	if compat.has_field(ALERT, "asset_register"):
		filter_sets.append({"asset_register": asset_name})
	if compat.has_field(ALERT, "source_doctype") and compat.has_field(ALERT, "source_docname"):
		filter_sets.append({"source_doctype": ASSET_REGISTER, "source_docname": asset_name})

	seen: dict = {}
	for filters in filter_sets:
		scoped = dict(filters)
		if compat.has_field(ALERT, "dismissed"):
			scoped["dismissed"] = 0
		try:
			rows = (
				frappe.db.get_all(
					ALERT, filters=scoped, fields=fields, order_by="due_date asc", limit=LINKED_CAP
				)
				or []
			)
		except Exception:  # pragma: no cover
			continue
		for raw in rows:
			row = dict(raw)
			due = str(row.get("due_date") or "") or None
			remaining = None
			if due:
				try:
					remaining = int(frappe.utils.date_diff(due, today))
				except Exception:  # pragma: no cover - an unparseable stored date
					remaining = None
			seen.setdefault(
				str(row.get("name")),
				{
					"name": row.get("name"),
					"alert_type": row.get("alert_type"),
					"severity": row.get("severity") or "Warning",
					"category": row.get("category") or "Other",
					"message": row.get("alert_message") or None,
					"status": row.get("status") or None,
					"due_date": due,
					"days_until_due": remaining,
					"overdue": bool(remaining is not None and remaining < 0),
					"first_seen": str(row.get("first_seen") or "") or None,
					"can_dismiss": bool(frappe.utils.cint(row.get("can_dismiss"))),
				},
			)
	return sorted(seen.values(), key=lambda row: str(row.get("due_date") or "9999-12-31"))


def _runtime(row: dict, today: str) -> dict:
	"""Today's, this week's and this season's minutes, for a valve.

	THREE WINDOWS, ONE PASS PER WINDOW over `irrigation._runs_for` — the same
	function `get_irrigation_runtime` and `get_water_usage_report` both build
	their totals from, so three surfaces cannot come to disagree about how long a
	gate was open. The valve's own row is measured plus everything below it,
	which is what makes a scan of a turnout report the line rather than the
	handle.
	"""
	from . import irrigation

	if not compat.doctype_exists(ASSET_STATE_LOG):
		return {}
	valves = irrigation._valves(row)
	if not valves:
		return {}

	now = _now()
	out: dict = {"valve_count": len(valves), "valves": [valve["name"] for valve in valves]}
	running: list[str] = []
	for key, days_back in RUNTIME_WINDOWS:
		start = str(frappe.utils.add_days(today, -days_back))
		opened, closed = f"{start} 00:00:00", f"{today} 23:59:59"
		try:
			measured = [irrigation._runs_for(valve, opened, closed, now) for valve in valves]
		except Exception:  # pragma: no cover - a log row nothing can parse
			continue
		minutes = round(sum(entry["runtime_minutes"] for entry in measured), 1)
		open_minutes = round(sum(entry["open_run_minutes"] for entry in measured), 1)
		out[f"{key}_minutes"] = minutes
		out[f"{key}_hours"] = round(minutes / 60.0, 2)
		out[f"{key}_open_run_minutes"] = open_minutes
		out[f"{key}_from"] = start
		# "Running now" is read off whichever window was measured last and is the
		# same answer from any of them — a valve with no closing row is open in
		# every window that reaches today. Collected inside the loop so a window
		# that failed to measure cannot leave this undefined.
		running = [entry["asset_name"] for entry in measured if entry["still_open"]]
	out["running_now"] = running
	out["is_running"] = bool(running)
	return out


def _parent_state(row: dict) -> dict:
	"""The valve above this one, and whether it is passing water.

	THE ONE PIECE OF CONTEXT A VALVE SCAN CANNOT DO WITHOUT. Closing a main shuts
	everything below it — `asset_tags._CASCADING_ACTIONS` is that rule — so a
	lateral reading `open` under a closed main is a valve with no water in it,
	and a worker sent to find out why nothing is running needs to be told about
	the handle three hundred yards uphill rather than discovering it.
	"""
	parent = str(row.get("location") or "")
	if not parent:
		return {}
	try:
		fields = compat.existing_fields(ASSET_REGISTER, ("name", "asset_type", "current_state", "retired_at"))
		found = frappe.db.get_value(ASSET_REGISTER, parent, fields, as_dict=True)
	except Exception:  # pragma: no cover
		return {}
	if not found:
		return {}
	found = dict(found)
	asset_type = str(found.get("asset_type") or "") or "General"
	defn = asset_tags._STATE_DEFINITIONS.get(asset_type)
	state = asset_tags._current_state_value(found.get("current_state")) or (defn["default"] if defn else "")
	out = {
		"parent_asset": found.get("name"),
		"parent_asset_type": asset_type,
		"parent_state": state or None,
		"parent_retired": bool(found.get("retired_at")),
	}
	if "open_valve" in ((defn or {}).get("actions") or {}) and state and state != "open":
		out["parent_blocking"] = True
		out["parent_note"] = (
			f"{found.get('name')} above this valve is {state!r}, so no water can reach here "
			"whatever this valve says. Open it first."
		)
	else:
		out["parent_blocking"] = False
	return out


def _location_blocks(row: dict) -> list[str]:
	"""Every block name this asset could be standing in, for the REI check.

	THREE ROUTES TO ONE ANSWER, because a farm maps its ground in more than one
	register and a restriction may have been recorded against any of them: the
	asset's own parent where that parent is a Block, the Irrigation Zone the
	asset draws through, and the planted Field that zone waters. All three are
	tried and the results are unioned — a restriction found under any of them is
	a real restriction on the ground under this machine.

	AN ASSET THAT IS ITSELF A BLOCK IS ONE OF THE ANSWERS. Scanning the gate tag
	of a block that was sprayed this morning must report the block as restricted,
	and that is this asset rather than anything above it.
	"""
	blocks = []
	if str(row.get("asset_type") or "") == "Block":
		blocks.append(str(row["name"]))

	parent = str(row.get("location") or "")
	if parent:
		try:
			parent_type = frappe.db.get_value(ASSET_REGISTER, parent, "asset_type")
		except Exception:  # pragma: no cover
			parent_type = None
		if str(parent_type or "") == "Block":
			blocks.append(parent)

	zone = str(row.get("irrigation_zone") or "")
	if zone and compat.doctype_exists(IRRIGATION_ZONE) and compat.has_field(IRRIGATION_ZONE, "field"):
		try:
			field = frappe.db.get_value(IRRIGATION_ZONE, zone, "field")
		except Exception:  # pragma: no cover
			field = None
		if field:
			blocks.append(str(field))

	return sorted(set(blocks))


# ── the composer ────────────────────────────────────────────────────────────
def status_report(row: dict, args: dict | None = None) -> dict:
	"""The whole picture for one asset, as a plain dict. Never raises.

	`row` is an Asset Register record as a dict — the caller has already read it
	(and, on the scan routes, already written the scan stamp onto it), so this
	takes the row rather than the docname and does not re-read what it was given.

	EVERY SECTION IS PRESENT ON EVERY ASSET, populated where it applies and empty
	where it does not. A handset renders one card per section and tests a count,
	rather than testing which keys arrived — the same promise `universal_scan`
	makes with `_EMPTY_STATE`, for the same reason.
	"""
	args = args or {}
	today = _today()
	name = str(row.get("name"))
	asset_type = str(row.get("asset_type") or "") or "General"
	unavailable: list[str] = []

	try:
		limit = int(args.get("history_limit") or ACTIVITY_LIMIT)
	except (TypeError, ValueError):
		limit = ACTIVITY_LIMIT
	limit = max(1, min(ACTIVITY_MAX, limit))

	def _section(label: str, produce, empty):
		try:
			return produce()
		except Exception as exc:  # pragma: no cover - reported, never raised
			unavailable.append(f"{label}: {type(exc).__name__}: {exc}")
			return empty

	activity = _section("recent_activity", lambda: _activity(name, limit), [])
	tasks = _section("open_tasks", lambda: _open_tasks(name), [])
	alerts = _section("compliance_alerts", lambda: _alerts(name, today), [])

	# v0.79.0. WHAT THE SCANNING WORKER WALKED AWAY FROM. Keyed on the caller
	# rather than on the asset, because it is a fact about the person holding the
	# phone and not about the machine they are pointing it at — and it belongs on
	# a scan for the reason `paused_tasks_for` gives: the scan is the moment they
	# have forgotten the irrigation line.
	worker = _scanning_worker(args)
	paused = _section("paused_tasks", lambda: paused_tasks_for(worker) if worker else [], [])

	# Sub-tasks of any open investigation or multi-day job on this asset, so a
	# scan of a machine an accident happened on shows what is still outstanding
	# rather than one Farm Task with nothing under it.
	children = _section("subtasks", lambda: _subtasks_for(tasks), {})

	hours = _section(
		"engine_hours",
		lambda: engine_hours.summary_for(name, args) if engine_hours.has_hour_meter(asset_type) else {},
		{},
	)
	service = _section(
		"maintenance",
		lambda: maintenance.status_for(row, today, hours_reading=hours.get("current_hours")),
		{},
	)

	runtime = _section(
		"runtime",
		lambda: _runtime(row, today) if asset_type in _valve_types() else {},
		{},
	)
	parent = _section("parent", lambda: _parent_state(row) if asset_type in _valve_types() else {}, {})

	blocks = _section("location_blocks", lambda: _location_blocks(row), [])
	location_reis = _section("location_reis", lambda: spray_rei.active_for_blocks(blocks), [])
	applied_reis = _section(
		"applied_reis",
		lambda: (
			[spray_rei._describe(entry, _now()) for entry in spray_rei.active_rows(sprayer=name)]
			if asset_type == "Sprayer"
			else []
		),
		[],
	)

	overdue_alerts = [alert for alert in alerts if alert["overdue"]]

	# ── the warning stack ────────────────────────────────────────────────────
	# ORDERED BY WHAT WOULD HURT SOMEBODY FIRST, and that ordering is the whole
	# value of assembling it here rather than leaving a client to sort five
	# lists. A restricted-entry interval is a person walking into a treated row;
	# everything below it is a machine or a deadline.
	warnings: list[dict] = []
	for rei in location_reis:
		warnings.append({"kind": "rei", "severity": "Critical", "message": rei["warning"]})
	for rei in applied_reis:
		warnings.append(
			{
				"kind": "rei_applied",
				"severity": "Warning",
				"message": (
					f"This sprayer applied {rei['product_name'] or 'a restricted product'} to "
					f"{rei['block']}; that block is closed until {rei['expires_at']} "
					f"({rei['hours_remaining']} h remaining)."
				),
			}
		)
	if service.get("due"):
		warnings.append({"kind": "maintenance", "severity": "Warning", "message": service["message"]})
	for alert in overdue_alerts:
		warnings.append(
			{
				"kind": "compliance",
				"severity": alert["severity"],
				"message": f"{alert['alert_type']} overdue since {alert['due_date']}.",
			}
		)
	if parent.get("parent_blocking"):
		warnings.append({"kind": "upstream", "severity": "Info", "message": parent["parent_note"]})

	for entry in paused:
		warnings.append(
			{
				"kind": "paused_task",
				"severity": "Info",
				"message": entry["message"],
				"message_key": entry["message_key"],
				"task": entry["task"],
			}
		)

	return {
		"status_report": True,
		"asset_name": name,
		"asset_type": asset_type,
		"generated_at": _now(),
		# ── state ────────────────────────────────────────────────────────────
		"state": _effective_state(row, asset_type),
		"last_state_change": activity[0] if activity else None,
		# ── service ──────────────────────────────────────────────────────────
		"maintenance": service,
		"maintenance_due": bool(service.get("due")),
		"maintenance_message": service.get("message") or None,
		# ── metering ─────────────────────────────────────────────────────────
		"engine_hours": hours,
		"current_hours": hours.get("current_hours"),
		"hours_this_season": hours.get("hours_this_season"),
		"hours_since_service": hours.get("hours_since_service"),
		"checked_out_now": bool(hours.get("checked_out_now")),
		# ── water ────────────────────────────────────────────────────────────
		"runtime": runtime,
		"parent_valve": parent,
		# ── work ─────────────────────────────────────────────────────────────
		"open_tasks": tasks,
		"open_task_count": len(tasks),
		"compliance_alerts": alerts,
		"compliance_alert_count": len(alerts),
		"overdue_alert_count": len(overdue_alerts),
		"recent_activity": activity,
		"recent_activity_count": len(activity),
		# ── what this worker left running ────────────────────────────────────
		"paused_tasks": paused,
		"paused_task_count": len(paused),
		# ── multi-day work under the tasks on this asset ─────────────────────
		"subtasks_by_parent": children,
		"open_subtask_count": sum(entry.get("open_subtask_count", 0) for entry in children.values()),
		# ── restricted entry ─────────────────────────────────────────────────
		"location_blocks": blocks,
		"active_reis": location_reis,
		"active_rei_count": len(location_reis),
		"applied_reis": applied_reis,
		"applied_rei_count": len(applied_reis),
		"rei_blocked": bool(location_reis),
		# ── the headline ─────────────────────────────────────────────────────
		"warnings": warnings,
		"warning_count": len(warnings),
		"needs_attention": bool(warnings),
		# A section that could not be read is NAMED rather than silently empty.
		# "no open tasks" and "the task register would not answer" are different
		# sentences, and only one of them is a reason to call somebody.
		"sections_unavailable": unavailable,
	}


def _scanning_worker(args: dict) -> str:
	"""The Employee holding the phone, from whatever the caller sent. Never raises.

	RESOLVED FROM `scanned_by` AS WELL AS FROM AN EXPLICIT EMPLOYEE, because
	`scan_asset` has always taken a USER — a login — and the paused-task lookup
	is keyed on an Employee. Making a handset send both would mean every client
	learning the mapping this app already knows.
	"""
	explicit = str(args.get("worker") or args.get("employee") or "").strip()
	if explicit:
		return explicit
	user = str(args.get("scanned_by") or args.get("user") or "").strip()
	if not user:
		return ""
	try:
		from . import fieldwork

		return str(fieldwork._employee_for(user) or "")
	except Exception:  # pragma: no cover - a site with no Employee register
		return ""


def _subtasks_for(tasks: list) -> dict:
	"""`{parent task: its step summary}` for the open tasks that have steps.

	ONLY THE PARENTS THAT HAVE CHILDREN. A scan already returns every open task
	on the asset; adding an empty step summary to each of them would be noise on
	the ordinary case, which is a job with no steps under it.
	"""
	from . import dispatch

	out = {}
	for task in tasks:
		summary = dispatch.subtask_summary(str(task.get("name") or ""))
		if summary.get("subtask_count"):
			out[str(task["name"])] = summary
	return out


def _valve_types() -> tuple[str, ...]:
	from . import irrigation

	return irrigation._valve_types()


def _effective_state(row: dict, asset_type: str) -> dict:
	"""The asset's state as a worker reads it, with the type's default applied."""
	defn = asset_tags._STATE_DEFINITIONS.get(asset_type)
	current = asset_tags._current_state_value(row.get("current_state"))
	effective = current or (defn["default"] if defn else "")
	return {
		"current_state": effective or None,
		"stated": bool(current),
		"default_state": (defn["default"] if defn else None),
		"last_scan_at": str(row.get("last_scan_at") or "") or None,
		"last_scan_by": row.get("last_scan_by") or None,
		"retired": bool(row.get("retired_at")),
	}


def report_for(asset_name: str, args: dict | None = None) -> dict:
	"""`status_report` from a docname, for callers that do not hold the row."""
	row = asset_tags.asset_row(asset_name)
	return status_report(row, args)


# ── get_asset_status_report ─────────────────────────────────────────────────
def get_asset_status_report(args: dict):
	"""The scan panel as a tool of its own, for a caller that is not scanning.

	SAME COMPOSER, NO WRITE. `scan_asset` stamps `last_scan_at` because somebody
	was standing there; a dispatcher checking a machine from a desk was not, and
	a register that recorded desk queries as field scans would make
	`last_scan_at` mean nothing.
	"""
	from ..args import as_str, resolve_company
	from ..result import ToolResult

	asset_tags._require()
	company = resolve_company(as_str(args, "company"))
	row = asset_tags.asset_row(as_str(args, "asset_name", required=True), company or "")
	report = status_report(row, args)
	described = asset_tags._describe_asset(row)

	headline = report["warnings"][0]["message"] if report["warnings"] else "nothing outstanding"
	return ToolResult(
		data={**described, **report},
		summary=f"{row['name']} ({report['asset_type']}): {headline}",
	)

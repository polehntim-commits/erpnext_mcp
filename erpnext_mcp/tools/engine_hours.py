# SPDX-License-Identifier: MIT
"""What the meter said, every time somebody took the machine out and brought it back.

`asset_actions._MENU` has carried a `log_hours` entry marked NOT BUILT since
v0.77.0, and the note under it is the design this file implements:

    Nothing on the Asset Register holds a meter reading, so there is nowhere to
    put the number and nothing to compare it against. Building it means one
    Float column for the last reading plus a log row per entry — the reading is
    only useful as a SERIES (hours since last service, hours this season), and a
    single overwritten column would answer none of those questions.

So there are two columns and they mean different things. `Asset State Log.engine_hours`
is THE RECORD: one reading per event, in a table that is append-only by its own
controller, and every figure below is arithmetic over that series.
`Asset Register.current_hours` is a CACHE of the highest reading seen — it is
what a scan shows without a second query, and nothing is computed from it. A
wrong value in the cache is a cosmetic error that the next reading corrects; a
wrong row in the series would be a wrong service interval, which is a warranty
claim.

WHY THE READING IS ATTACHED TO A STATE CHANGE RATHER THAN BEING ITS OWN CALL.
The moment somebody actually reads a tractor's hour meter is the moment they are
sitting in it — checking it out at the yard or bringing it back. A separate
"log hours" call is one more thing to remember at the end of a fourteen-hour day,
and the number that gets remembered is the one the machine is already asking for.
So `check_out` and `check_in` take `engine_hours`, and a session's hours are the
difference between the pair.

A METER ONLY COUNTS UP, and this file enforces that. A reading lower than the
last one on record is refused by default, because the two things that produce
one are a typo (`1240` for `12400`) and a replaced instrument — and a typo
accepted quietly turns into "this tractor has run negative hours this season",
which nobody reads as a data error. `allow_meter_reset=true` is how somebody
says they meant it, and the reset is recorded in the log row's notes so the
discontinuity in the series has an explanation attached to it.

HOURS PER SESSION ARE COMPUTED ONLY WHERE BOTH ENDS EXIST. A checkout nobody
metered followed by a metered check-in leaves `hours_used` empty rather than
inventing a session length from the last reading of any kind — which on a
machine that was serviced in between would bill the service bay's test run to a
worker's afternoon.
"""

from __future__ import annotations

import frappe

from .. import compat, timezones
from ..args import as_bool, as_date, as_float, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import asset_tags

ASSET_REGISTER = asset_tags.ASSET_REGISTER
ASSET_STATE_LOG = asset_tags.ASSET_STATE_LOG

#: The pair of actions that make an asset type a metered machine. TESTED ON THE
#: ACTION NAMES rather than listed by type, exactly as `irrigation._valve_types`
#: is and for the same reason: a machine somebody takes out and brings back is
#: one whose running time is a question, and a type that grows that pair later
#: starts being metered without an edit in this file.
_METER_ACTIONS = ("check_out", "check_in")

#: Which of those two OPENS a session and which CLOSES it. A reading on the
#: opening event is a datum; a reading on the closing one is a datum AND a
#: subtraction.
OPENING_ACTION = "check_out"
CLOSING_ACTION = "check_in"

#: How far a reading may fall below the last one on record before it is refused
#: as a mistake. Not zero: an hour meter reading to one decimal, re-read by
#: somebody squinting at a dusty gauge, legitimately comes back a tenth low, and
#: refusing that would train people to stop entering readings at all.
METER_TOLERANCE_HOURS = 0.2

#: Most log rows one summary reads. A machine with more metered events than this
#: in one window is an automation writing rows; the cap is reported rather than
#: quietly shortening a season's hours.
EVENT_CAP = 2000

#: Most sessions itemised in the answer. The totals are computed from everything.
SESSION_CAP = 200

_LOG_FIELDS = (
	"name",
	"action",
	"from_state",
	"to_state",
	"performed_by",
	"performed_at",
	"engine_hours",
	"hours_used",
	"notes",
	"creation",
)


def metered_types() -> tuple[str, ...]:
	"""Which asset types have a check-out/check-in pair, and so have hours."""
	return tuple(
		asset_type
		for asset_type, defn in asset_tags._STATE_DEFINITIONS.items()
		if all(action in defn["actions"] for action in _METER_ACTIONS)
	)


def is_metered(asset_type: str) -> bool:
	return str(asset_type or "") in metered_types()


#: v0.198.0. MACHINES WITH AN HOUR METER THAT NOBODY CHECKS OUT. A wind machine
#: runs on cold nights with nobody in it and a pump runs when the zone is open;
#: both have a meter on the housing and both are serviced on hours, but neither
#: has a check-out/check-in pair, so `metered_types` never counted them and the
#: reading asked for on 40-WM-SE's inspection (FT-2026-09-00004) had nowhere to
#: go. Named here rather than grown as a column on Farm Asset Type: five types,
#: all named the way the seeded register names them, and a site with a type of
#: its own that has a meter adds it in one line.
READING_TYPES = ("Wind Machine", "Pump", "Irrigation Pump", "Well Pump", "Generator")

#: The action a reading taken at a task's completion is filed under. It is the
#: `log_hours` entry `asset_actions` has described since v0.77.0: a reading on
#: its own, with the state unchanged.
READING_ACTION = "log_hours"


def hour_meter_types() -> tuple[str, ...]:
	"""Every asset type with an hour meter: the checked-out ones and `READING_TYPES`."""
	return tuple(dict.fromkeys((*metered_types(), *READING_TYPES)))


def has_hour_meter(asset_type: str) -> bool:
	return str(asset_type or "") in hour_meter_types()


def asset_hours(asset_name: str) -> dict | None:
	"""What a phone needs to ask for a reading: whether there is a meter, and the last figure.

	None for no asset. The register's `current_hours` is the CACHE — right for a
	screen showing "last read 1,240 h", which is all this is for.
	"""
	name = str(asset_name or "").strip()
	if not name or not compat.doctype_exists(ASSET_REGISTER):
		return None
	fields = compat.existing_fields(
		ASSET_REGISTER, ("name", "asset_type", "current_hours", "hours_updated_at")
	)
	row = frappe.db.get_value(ASSET_REGISTER, name, fields, as_dict=True)
	if not row:
		return None
	asset_type = str(row.get("asset_type") or "")
	return {
		"asset": row["name"],
		"asset_type": asset_type or None,
		"has_hour_meter": has_hour_meter(asset_type),
		"current_hours": float(row.get("current_hours") or 0),
		"hours_updated_at": str(row["hours_updated_at"]) if row.get("hours_updated_at") else None,
	}


def record_completion_reading(
	asset_name: str,
	value,
	*,
	performed_by: str,
	task: str,
	allow_meter_reset: bool = False,
	latitude=None,
	longitude=None,
) -> dict:
	"""File a reading taken when a task on this machine was completed.

	THE SAME PATH AS A CHECK-IN'S READING, NOT A SECOND ONE: `apply_reading`
	validates it, an Asset State Log row carries it (the series is the record),
	and `cache_reading` moves `current_hours` / `hours_updated_at`. The row's
	action is `log_hours` and its state is unchanged — reading a meter does not
	move a machine.

	A READING BELOW THE LAST ONE DOES NOT RAISE HERE. This runs inside a
	completion that may have sat in a handset's queue since last night, and a
	typo must not strand the work: the refusal `apply_reading` would give is
	returned as `recorded: false` with its sentence, and the completion goes on.
	A value that is not a number at all was refused before anything was written
	— see `dispatch.complete_farm_task`.
	"""
	answer = {
		"recorded": False,
		"asset": asset_name,
		"engine_hours": _reading(value),
		"previous_reading": None,
		"state_log": None,
		"reason": None,
	}
	row = asset_tags.asset_row(asset_name)
	asset_type = str(row.get("asset_type") or "")
	try:
		meter = apply_reading(
			dict(row),
			asset_type,
			READING_ACTION,
			{"engine_hours": value, "allow_meter_reset": allow_meter_reset},
		)
	except ToolError as exc:
		answer["reason"] = str(exc).replace(" Nothing was recorded.", "") + (
			" The completion was filed; the reading was not."
		)
		return answer
	if not meter:
		return answer

	state = _current_state(row)
	log = frappe.new_doc(ASSET_STATE_LOG)
	log.asset_name = row["name"]
	log.asset_type = asset_type
	log.action = READING_ACTION
	log.from_state = state
	log.to_state = state
	log.performed_by = performed_by or frappe.session.user
	log.performed_at = frappe.utils.now()
	log.engine_hours = meter["engine_hours"]
	notes = f"Hour meter read at completion of {task}."
	if meter.get("meter_note"):
		notes += " " + meter["meter_note"]
	log.notes = notes
	for column, raw in (("gps_latitude", latitude), ("gps_longitude", longitude)):
		if raw not in (None, "") and compat.has_field(ASSET_STATE_LOG, column):
			try:
				log.set(column, float(raw))
			except (TypeError, ValueError):
				pass
	log.insert(ignore_permissions=True)
	cache_reading(row["name"], meter["engine_hours"], reset=meter["meter_reset"])
	answer.update(
		recorded=True,
		engine_hours=meter["engine_hours"],
		previous_reading=meter.get("previous_reading"),
		state_log=log.name,
	)
	return answer


def _current_state(row: dict) -> str:
	"""The asset's state as its register row stores it, or ""."""
	raw = row.get("current_state")
	if isinstance(raw, dict):
		return str(raw.get("state") or "")
	try:
		import json

		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
	except ValueError:
		return ""
	return str(value.get("state") or "") if isinstance(value, dict) else ""


def _stamp(row: dict) -> str:
	"""When the event happened — `performed_at`, or the row's own creation.

	The same fallback `list_asset_state_history` and `irrigation._stamp` report
	on, so a machine's hours and a valve's minutes are ordered by one rule.
	"""
	return str(row.get("performed_at") or row.get("creation") or "")


def _reading(value) -> float | None:
	"""A meter reading as a number, or None where none was sent.

	ZERO IS A READING. A machine straight off the lot reads 0.0, and `or None`
	on that would drop the one datum that makes the first session computable.
	"""
	if value in (None, ""):
		return None
	try:
		return round(float(value), 1)
	except (TypeError, ValueError):
		raise ToolError(
			f"engine_hours must be a number, got {value!r}. An hour meter reads a decimal like "
			"1240.5. Nothing was recorded."
		) from None


def last_reading(asset_name: str) -> dict:
	"""The most recent metered event on this machine, or `{}`.

	Read off the LOG rather than off `Asset Register.current_hours`, because the
	cache is a cache — see the module docstring. The one place the register's
	column is authoritative is a screen, and a screen is not doing arithmetic.
	"""
	if not compat.doctype_exists(ASSET_STATE_LOG) or not compat.has_field(ASSET_STATE_LOG, "engine_hours"):
		return {}
	try:
		rows = (
			frappe.db.get_all(
				ASSET_STATE_LOG,
				filters={"asset_name": asset_name, "engine_hours": (">", 0)},
				fields=compat.existing_fields(ASSET_STATE_LOG, _LOG_FIELDS),
				order_by="creation desc",
				limit=1,
			)
			or []
		)
	except Exception:  # pragma: no cover - a site shaping these columns differently
		return {}
	return dict(rows[0]) if rows else {}


def _open_checkout(asset_name: str) -> dict:
	"""The metered `check_out` this check-in is closing, or `{}`.

	WALKS BACK FROM THE NEWEST EVENT AND STOPS AT THE FIRST CHECK-OUT, rather
	than querying for the latest check-out directly. The difference matters on a
	machine that went out, came back, and went out again: the session being
	closed is the LAST checkout, and anything filed after it — a maintenance
	start, a fault flag — does not end the session but does mean the query has to
	be ordered rather than filtered.

	Returns `{}` where the opening event carried no reading, which is what makes
	`hours_used` empty rather than invented.
	"""
	if not compat.doctype_exists(ASSET_STATE_LOG):
		return {}
	try:
		rows = (
			frappe.db.get_all(
				ASSET_STATE_LOG,
				filters={"asset_name": asset_name},
				fields=compat.existing_fields(ASSET_STATE_LOG, _LOG_FIELDS),
				order_by="creation desc",
				limit=EVENT_CAP,
			)
			or []
		)
	except Exception:  # pragma: no cover
		return {}
	for row in rows:
		row = dict(row)
		action = str(row.get("action") or "")
		if action == CLOSING_ACTION:
			# A CHECK-IN REACHED BEFORE ANY CHECK-OUT MEANS THERE IS NO OPEN
			# SESSION, and this stops rather than keeps looking. Walking past it
			# would find the check-out that this check-in already closed and
			# measure a second session across the same hours — which the state
			# machine makes unreachable today (a check-in needs `checked_out`),
			# and which a future state that reached `checked_out` some other way
			# would open up silently.
			return {}
		if action == OPENING_ACTION:
			return row if _reading(row.get("engine_hours")) is not None else {}
	return {}


def apply_reading(row: dict, asset_type: str, action: str, args: dict) -> dict:
	"""Validate the meter reading and say what it produced. Writes nothing.

	CALLED FROM `asset_tags._write_state_change` AND NOWHERE ELSE, so a reading
	can only ever arrive attached to a real, validated state change. The
	alternative — a `log_engine_hours` tool of its own — would have been a second
	door onto the same series with none of the state machine's checks in front of
	it, and a reading filed against a machine nobody had checked out.

	PURE ON PURPOSE. It returns the columns the caller should set rather than
	setting them, so every refusal below happens BEFORE the state change is
	written — somebody who fat-fingered the meter meant to make one record, not
	half of one, and a tractor checked in with a rejected reading and no
	explanation is the worse of the two failures.

	Returns `{}` when there is nothing to record, so the caller writes nothing.
	"""
	value = _reading(args.get("engine_hours"))
	if value is None:
		return {}
	if not compat.has_field(ASSET_STATE_LOG, "engine_hours"):
		raise ToolError(
			"this site's Asset State Log has no engine_hours column, so a meter reading has "
			"nowhere to go — run `bench --site <site> migrate` after upgrading the app. Nothing "
			"was recorded."
		)
	if not has_hour_meter(asset_type):
		raise ToolError(
			f"{row['name']} is a {asset_type!r}, which has no hour meter — engine_hours is "
			f"accepted on: {', '.join(hour_meter_types())}. Nothing was recorded."
		)
	if value < 0:
		raise ToolError(f"engine_hours cannot be negative, got {value}. Nothing was recorded.")

	previous = last_reading(row["name"])
	prior = _reading(previous.get("engine_hours")) if previous else None
	reset = bool(as_bool(args, "allow_meter_reset", False))
	note = ""
	if prior is not None and value < prior - METER_TOLERANCE_HOURS:
		if not reset:
			raise ToolError(
				f"engine_hours of {value} is below the {prior} already on record for "
				f"{row['name']}. An hour meter counts up, so this is either a typo or a replaced "
				"instrument. Check the reading; if the meter really was reset or swapped, send "
				"allow_meter_reset=true and the discontinuity is recorded with the row. Nothing "
				"was recorded."
			)
		note = (
			f"Meter reset accepted: read {value} where {prior} was on record. Hours before this "
			"point are on the previous instrument and do not add up across it."
		)

	used = None
	if action == CLOSING_ACTION and not note:
		opening = _open_checkout(row["name"])
		start = _reading(opening.get("engine_hours")) if opening else None
		if start is not None and value >= start:
			used = round(value - start, 1)

	return {
		"engine_hours": value,
		"previous_reading": prior,
		"hours_used": used,
		"meter_reset": bool(note),
		"meter_note": note or None,
	}


def cache_reading(asset_name: str, value: float, reset: bool = False) -> None:
	"""Point `Asset Register.current_hours` at the newest reading. Never raises.

	Only ever moves FORWARD unless a reset was declared, for the reason the
	refusal above exists: the register's column is what a scan shows, and a scan
	that showed a lower figure than the machine has actually run would send
	somebody looking for a service that is already overdue.

	Failures here are swallowed. The reading is already in the log, which is the
	record; losing a state change because a cache would not write would be the
	wrong trade.
	"""
	try:
		if not compat.has_field(ASSET_REGISTER, "current_hours"):
			return
		current = frappe.db.get_value(ASSET_REGISTER, asset_name, "current_hours")
		try:
			current = float(current or 0)
		except (TypeError, ValueError):
			current = 0.0
		if not reset and value < current:
			return
		updates = {"current_hours": value}
		if compat.has_field(ASSET_REGISTER, "hours_updated_at"):
			updates["hours_updated_at"] = frappe.utils.now()
		frappe.db.set_value(ASSET_REGISTER, asset_name, updates, update_modified=False)
	except Exception:  # pragma: no cover - a cache, never a refusal
		return


# ── the series ──────────────────────────────────────────────────────────────
def _events(asset_name: str, since: str = "") -> tuple[list[dict], bool]:
	"""Every metered event on this machine, oldest first.

	Ordered on `creation` in the query and re-sorted on the stamp in Python, for
	the reason `irrigation._events` gives: the column a database orders cheaply
	is the one that is always set, and the column the arithmetic needs is the one
	the worker's handset stamped. On a queued check-in filed hours later those
	two disagree, and getting it wrong is a negative session.
	"""
	if not compat.doctype_exists(ASSET_STATE_LOG) or not compat.has_field(ASSET_STATE_LOG, "engine_hours"):
		return [], False
	filters: dict = {"asset_name": asset_name, "engine_hours": ("is", "set")}
	if since:
		column = "performed_at" if compat.has_field(ASSET_STATE_LOG, "performed_at") else "creation"
		filters[column] = (">=", since)
	try:
		rows = (
			frappe.db.get_all(
				ASSET_STATE_LOG,
				filters=filters,
				fields=compat.existing_fields(ASSET_STATE_LOG, _LOG_FIELDS),
				order_by="creation asc",
				limit=EVENT_CAP + 1,
			)
			or []
		)
	except Exception:  # pragma: no cover
		return [], False
	truncated = len(rows) > EVENT_CAP
	kept = [dict(row) for row in rows[:EVENT_CAP] if _reading(row.get("engine_hours")) is not None]
	return sorted(kept, key=_stamp), truncated


def _sessions(events: list[dict]) -> list[dict]:
	"""Pair check-outs with check-ins and report the hours between them.

	COMPUTED HERE RATHER THAN READ OFF `hours_used`, even though the column is
	written at check-in. The stored value is what a client shows on one row; this
	is the arithmetic a total is built from, and building the total from stored
	values would mean a single row written before the column existed silently
	dropped a session out of a season.

	An OPEN session — checked out and not yet back — is reported with no hours on
	it rather than measured against the machine's current reading. Nobody has
	read the meter since it left the yard, so there is no second number to
	subtract; a session in progress has a start and no length.
	"""
	sessions: list[dict] = []
	open_event: dict = {}
	for event in events:
		action = str(event.get("action") or "")
		reading = _reading(event.get("engine_hours"))
		if action == OPENING_ACTION:
			open_event = event
			continue
		if action != CLOSING_ACTION or not open_event:
			continue
		start = _reading(open_event.get("engine_hours"))
		hours = None
		if start is not None and reading is not None and reading >= start:
			hours = round(reading - start, 1)
		sessions.append(
			{
				"checked_out_at": _stamp(open_event),
				"checked_in_at": _stamp(event),
				"start_hours": start,
				"end_hours": reading,
				"hours": hours,
				"checked_out_by": open_event.get("performed_by") or None,
				"checked_in_by": event.get("performed_by") or None,
				"open": False,
			}
		)
		open_event = {}

	if open_event:
		sessions.append(
			{
				"checked_out_at": _stamp(open_event),
				"checked_in_at": None,
				"start_hours": _reading(open_event.get("engine_hours")),
				"end_hours": None,
				"hours": None,
				"checked_out_by": open_event.get("performed_by") or None,
				"checked_in_by": None,
				"open": True,
			}
		)
	return sessions


def _season_start(args: dict) -> str:
	"""The date "this season" is counted from.

	THE CALENDAR YEAR BY DEFAULT, AND STATED RATHER THAN ASSUMED. This app has no
	Season doctype and inventing one to hold a single date would be a table for a
	preference. A tree-fruit operation whose machine year does not start on 1
	January passes `season_start` and gets its own answer; every figure in the
	result says which date it was measured from, so nobody has to guess whether
	the default applied.
	"""
	stated = as_date(args, "season_start")
	if stated:
		return stated
	return f"{str(frappe.utils.today())[:4]}-01-01"


def summary_for(asset_name: str, args: dict | None = None) -> dict:
	"""The hours block, as a plain dict. Shared with the scan status report.

	Returns `{"metered": False, ...}` for a machine with no meter rather than
	raising, because this is called from a scan of anything — the caller has a
	valve as often as a tractor, and a scan is not the place for a refusal about
	the kind of thing that was scanned.
	"""
	args = args or {}
	row = asset_tags.asset_row(asset_name)
	asset_type = str(row.get("asset_type") or "") or "General"
	season = _season_start(args)

	if not has_hour_meter(asset_type):
		return {
			"asset_name": row["name"],
			"asset_type": asset_type,
			"metered": False,
			"note": (
				f"{asset_type} has no hour meter — hours are recorded on: {', '.join(hour_meter_types())}."
			),
			"current_hours": None,
			"season_start": season,
		}

	events, truncated = _events(row["name"])
	sessions = _sessions(events)
	closed = [session for session in sessions if not session["open"] and session["hours"] is not None]

	readings = [_reading(event.get("engine_hours")) for event in events]
	readings = [value for value in readings if value is not None]
	current = max(readings) if readings else None
	first = min(readings) if readings else None

	season_events = [event for event in events if _stamp(event)[:10] >= season]
	season_sessions = [
		session
		for session in _sessions(season_events)
		if not session["open"] and session["hours"] is not None
	]

	try:
		last_service_hours = float(row.get("last_service_hours") or 0) or None
	except (TypeError, ValueError):
		last_service_hours = None
	since_service = (
		round(current - last_service_hours, 1)
		if current is not None and last_service_hours is not None
		else None
	)

	open_session = next((session for session in sessions if session["open"]), None)

	return {
		"asset_name": row["name"],
		"asset_type": asset_type,
		"metered": True,
		# THE MACHINE'S OWN NUMBER, off the series. `Asset Register.current_hours`
		# is reported beside it rather than instead of it, so a cache that has
		# drifted is visible rather than authoritative.
		"current_hours": current,
		"cached_current_hours": (
			round(float(row.get("current_hours") or 0), 1) if row.get("current_hours") else None
		),
		"first_reading": first,
		"total_hours_recorded": (
			round(current - first, 1) if current is not None and first is not None else None
		),
		"session_count": len(closed),
		"session_hours_total": round(sum(session["hours"] for session in closed), 1),
		"season_start": season,
		"season_session_count": len(season_sessions),
		"hours_this_season": round(sum(session["hours"] for session in season_sessions), 1),
		"last_service_hours": last_service_hours,
		"hours_since_service": since_service,
		"service_interval_hours": (round(float(row.get("service_interval_hours") or 0), 1) or None),
		"checked_out_now": bool(open_session),
		"open_session": open_session,
		"reading_count": len(readings),
		"events_truncated": truncated,
		"sessions": sessions[-SESSION_CAP:],
	}


# ── get_engine_hours_summary ────────────────────────────────────────────────
def get_engine_hours_summary(args: dict) -> ToolResult:
	"""Total hours, hours this season, and hours since the last service."""
	asset_tags._require()
	company = resolve_company(as_str(args, "company"))
	row = asset_tags.asset_row(as_str(args, "asset_name", required=True), company or "")
	data = summary_for(row["name"], args)

	limit = as_limit(args)
	data["sessions"] = data.get("sessions", [])[-min(limit, SESSION_CAP) :]

	clock = timezones.Renderer(args)
	for session in data["sessions"]:
		clock.add(session, "checked_out_at", "checked_in_at")
	if data.get("open_session"):
		clock.add(data["open_session"], "checked_out_at", "checked_in_at")
	data["company"] = row.get("company") or None
	data.update(clock.block())

	if not data["metered"]:
		return ToolResult(data=data, summary=f"{row['name']}: {data['note']}")

	return ToolResult(
		data=data,
		summary=(
			f"{row['name']}: {data['current_hours'] if data['current_hours'] is not None else '—'} h "
			f"on the meter, {data['hours_this_season']} h this season, "
			+ (
				f"{data['hours_since_service']} h since service"
				if data["hours_since_service"] is not None
				else "no service reading on record"
			)
			+ (", checked out now" if data["checked_out_now"] else "")
		),
	)


# ── record_service ──────────────────────────────────────────────────────────
def record_service(args: dict) -> ToolResult:
	"""Mark a service done: stamp the date and the meter it was done at.

	THE OTHER HALF OF THE SCHEDULE. `check_maintenance_due` reads
	`last_service_date` and `last_service_hours`, and until something writes them
	every machine on the farm is overdue from the day it was registered — which
	is the state that trains people to ignore the alert.

	The meter reading defaults to whatever the series already says the machine
	has run, so the ordinary call is the asset's name and nothing else.
	"""
	asset_tags._require()
	company = resolve_company(as_str(args, "company"))
	row = asset_tags.asset_row(as_str(args, "asset_name", required=True), company or "")

	doc = frappe.get_doc(ASSET_REGISTER, row["name"])
	service_date = as_date(args, "service_date") or str(frappe.utils.today())

	stated = args.get("service_hours")
	if stated is not None:
		hours = as_float(stated, "service_hours")
	else:
		summary = summary_for(row["name"], args)
		hours = summary.get("current_hours")

	before = {
		"last_service_date": str(doc.get("last_service_date") or "") or None,
		"last_service_hours": (
			round(float(doc.get("last_service_hours") or 0), 1) if doc.get("last_service_hours") else None
		),
	}

	doc.last_service_date = service_date
	if hours is not None:
		doc.last_service_hours = round(float(hours), 1)
	notes = as_str(args, "notes")
	if notes:
		doc.description = (
			f"{doc.description}\n\n{service_date} service: {notes}".strip()
			if doc.description
			else f"{service_date} service: {notes}"
		)
	doc.save(ignore_permissions=True)

	return ToolResult(
		data={
			"asset_name": doc.name,
			"asset_type": doc.asset_type,
			"service_date": service_date,
			"service_hours": (
				round(float(doc.last_service_hours or 0), 1) if doc.last_service_hours else None
			),
			"previous": before,
			"notes": notes or None,
		},
		summary=(
			f"{doc.name}: service recorded {service_date}"
			+ (f" at {doc.last_service_hours} h" if doc.last_service_hours else "")
		),
		docstatus_delta="0 → 0 (updated)",
	)

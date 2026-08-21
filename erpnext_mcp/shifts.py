# SPDX-License-Identifier: MIT
"""The shift: what one is, how it is read, and the Attendance it produces.

v0.19.3. THE ARCHITECTURAL MOVE OF THE RELEASE IS THAT COMPLIANCE ANCHORS TO A
SHIFT RATHER THAN TO A TASK, and this module is the single place that knows what
that means, for the same reason `training.py` exists: the controller, the five
mutating tools, the four reads, the compliance rule and the audit packet builder
must not be able to disagree about what a shift's crew was or how long somebody
was on it.

────────────────────────────────────────────────────────────────────────────
WHY THE SHIFT AND NOT THE TASK
────────────────────────────────────────────────────────────────────────────

A task completion carries a point-in-time reading. A shift carries a timeline.
Oregon OSHA does not ask what the temperature was when one job closed; it asks
whether the July 15 shift complied with OAR 437-004-1131 from start to finish —
water at the required rate for the whole exposure, shade within reach, rest
cycles enforced once the heat index passed 80 °F, the supervisor observing for
signs. Every one of those is a statement about a PERIOD and about a CREW, and a
record scoped to a single job cannot make it.

The same shape serves the other exposure regimes without being redesigned. A
spray shift is a wind-speed timeline plus product and PPE, which is what WPS
asks for. A frost-protection irrigation shift is a soil-temperature timeline
plus valve changes. A ten-hour harvest is heat and hydration. One doctype, one
timeline, three regimes.

────────────────────────────────────────────────────────────────────────────
THE FOREMAN IS THE SOLE ACTOR, AND THAT IS A COMPLIANCE DECISION
────────────────────────────────────────────────────────────────────────────

Workers do not self-clock into a shift. The foreman forms the crew, adds and
removes people, logs the events and signs the close-out. Three reasons, and the
first is the one that decides it:

  * -1131 puts the obligations on a named responsible person — the one who calls
    the water break, who observes for signs, who decides a rest cycle is needed.
    The record has to carry that person's identity as a fact, not derive it.
  * FSMA §112.161(b) asks a supervisor to review and sign. Their identity must be
    ON the shift rather than inferred from whoever happened to be rostered.
  * Worker self-clock is the failure mode where nobody logged the water break
    because everybody assumed somebody else had. A crew of thirty with thirty
    people responsible for the record has nobody responsible for the record.

Some workers will not have a device in the field at all, which makes the point
moot in practice as well as in principle.

────────────────────────────────────────────────────────────────────────────
PER-WORKER ATTENDANCE SURVIVES INSIDE THE CREW ENVELOPE
────────────────────────────────────────────────────────────────────────────

The obvious objection to a crew-shaped record is that payroll is person-shaped,
and it would be a real objection if the crew table were a list of names. It is
not: every row carries `joined_at` and `left_at`, so "the crew worked 06:00 to
15:00" and "Ana joined at 07:10 and left at 13:00" are both true, both stored,
and neither derived from the other.

`remove_worker_from_shift` SETS `left_at`; it does not delete the row. Deleting
would destroy the only record that the person was on the shift at all, which is
the record a wage claim turns on.

────────────────────────────────────────────────────────────────────────────
THE ATTENDANCE BRIDGE RUNS ONE WAY, ON PURPOSE
────────────────────────────────────────────────────────────────────────────

When a shift closes, `bridge_to_attendance` writes one submitted Frappe HR
`Attendance` per crew member, spanning that person's own `joined_at` to their
own `left_at` — or to the shift's end where they stayed to it. So farm_hr keeps
one canonical answer to "when was Ana at work" without the shift stopping being
a single record, and `get_attendance_summary` counts a shift-formed day exactly
as it counts a hand-entered one.

IT DOES NOT RUN THE OTHER WAY, and that is not an omission. A shift is formed by
a foreman naming a crew, a location and a type; an Attendance row carries none of
those. Deriving shifts from attendance would invent a foreman, invent a location
and invent the boundary between two crews who worked the same day — three
fabrications on a record an inspector reads. Sites with existing attendance keep
it; shifts start when somebody starts forming them.

The bridge NEVER RAISES INTO THE CLOSE. A site without Frappe HR has no
Attendance doctype, an employee may have been archived since the shift ran, and
a duplicate row for a day somebody already keyed in by hand is a real
possibility. None of those is a reason to refuse to close a shift whose
supervisor has signed it — the signature is the compliance act and the payroll
row is the convenience — so every failure is REPORTED in the close-out's result
and none of them stops it.
"""

from __future__ import annotations

import datetime as _dt

import frappe

from . import breaks as breaks_mod
from . import compat
from . import minors as minors_mod

DOCTYPE = "Farm Shift"
CREW_DOCTYPE = "Farm Shift Crew Member"
EVENT_DOCTYPE = "Farm Shift Compliance Event"
WEATHER_DOCTYPE = "Farm Shift Weather Reading"
#: v0.32.0. The crew's track. NOT a child table of the shift, and
#: `shift_location_log.py` argues why at length — the short version is that a
#: nine-hour shift at a fix every two minutes is two hundred and seventy rows,
#: and a child table is loaded whole every time anybody opens the shift form.
LOCATION_DOCTYPE = "Shift Location Log"
HEAT_DOCTYPE = "Heat Exposure Event"
ACCLIMATIZATION_DOCTYPE = "Heat Acclimatization Worker"

ATTENDANCE_DOCTYPE = "Attendance"

#: The Custom Field the bridge writes on Attendance. Installed by
#: `compliance_fields.py` alongside the v0.15.0 fields, which is where every
#: column this app grafts onto somebody else's doctype is declared and where
#: `before_uninstall` finds it to warn about.
ATTENDANCE_SHIFT_FIELD = "farm_shift"

STATUS_ACTIVE = "Active"
STATUS_CLOSED = "Closed"
STATUS_CANCELLED = "Cancelled"

#: The Attendance status a shift-formed day produces. Present, always: a person
#: on a crew list was at work, and any other status is a fact somebody has to
#: assert rather than one a closed shift proves.
ATTENDANCE_STATUS = "Present"

#: The heat index Oregon's rule engages at, and the one it adds obligations at.
#: Here rather than in the rule that reads them, because v0.19.4's threshold hook
#: and the Heat Exposure Event's own notes both quote the same numbers, and a
#: threshold that lives in two files is a threshold that changes in one.
HEAT_THRESHOLD_F = 80.0
HEAT_HIGH_THRESHOLD_F = 90.0

#: OAR 437-004-1131(g): fewer than this many days working in the heat and the
#: worker needs an acclimatization plan.
ACCLIMATIZATION_DAYS = 14

CITATION = "OAR 437-004-1131"


# ── naming ──────────────────────────────────────────────────────────────────
def next_in_series(doctype: str, prefix: str, segment: str, width: int = 4) -> str:
	"""`PREFIX-SEGMENT-0001`, counted from what is already on the site.

	EXPANDED HERE RATHER THAN BY FRAPPE'S `naming_series` HOOK. Both doctypes
	declare their series as a field so the Desk shows it and an operator can read
	what the shape is meant to be, but the expansion is this function — which
	means a docname is the same string on a bench, in a patch, and in the
	standalone suite, and a test asserting `SHIFT-2026-0001` is asserting the
	thing that ships.

	`segment` IS A YEAR ON EVERY DATED REGISTER and a company abbreviation on
	Scale Ticket and Settlement Statement — the counting is the same either way,
	because what it counts is whatever already sits between the prefix and the
	sequence. It was called `year` until v0.67.0, when the two receipt registers
	arrived wanting `ST-OML-0001`; the parameter was renamed rather than the
	function copied, because a second copy of "read every existing name and take
	the highest" is a second place for the off-by-one to live.

	Where the segment IS a year it is the record's own year, not this year. A
	shift that ran on 31 December and was closed on 1 January belongs to the year
	it started, and a series keyed off `today()` would file it under the wrong one
	for ever.
	"""
	head = f"{prefix}-{segment}-"
	existing = frappe.db.get_all(doctype, filters={"name": ("like", f"{head}%")}, pluck="name", limit=100000)
	highest = 0
	for name in existing or []:
		tail = str(name).rsplit("-", 1)[-1]
		if tail.isdigit():
			highest = max(highest, int(tail))
	return f"{head}{highest + 1:0{width}d}"


# ── status ──────────────────────────────────────────────────────────────────
def status_for(end_datetime, cancelled=False) -> str:
	"""Active, Closed or Cancelled, from the two facts that decide it.

	No end time means the shift is still running, whatever anybody ticked — an
	open shift is what the v0.19.4 weather sweep walks, and a shift marked Closed
	with no end would silently drop out of the fetch while still being worked.
	That ordering is the whole of the rule.
	"""
	if not str(end_datetime or "").strip():
		return STATUS_ACTIVE
	return STATUS_CANCELLED if compat.checked(cancelled) else STATUS_CLOSED


def is_open(row: dict) -> bool:
	return not str(row.get("end_datetime") or "").strip()


def lock_shift(name: str) -> None:
	"""Hold the Farm Shift row until this transaction ends. THE ROSTER RACE'S FIX.

	────────────────────────────────────────────────────────────────────────
	TWO BADGES, ONE SHIFT, AND ONE OF THEM OFF THE PAYROLL
	────────────────────────────────────────────────────────────────────────

	`add_worker_to_shift` reads the shift, walks `crew` for a name already on it,
	appends a row and saves — four statements with three gaps in them. Frappe
	rewrites a child table by DELETING its rows and re-inserting them, so two
	foremen scanning two different badges onto one crew in the same moment both
	load a crew of N, both write a crew of N+1, and the second commit leaves the
	FIRST worker's row gone. Nothing afterwards shows there were two scans: the
	shift has a plausible crew, the phone that scanned first got a 200, and the
	person it dropped is picking in the block with no Attendance row and no
	payroll day. That is a wage liability produced by a scan that appeared to
	succeed, which is the worst shape a defect of this kind takes.

	THE DUPLICATE GUARD IS RACED THE SAME WAY, in the other direction. Two scans
	of the SAME badge — the ordinary double-tap on a phone with a slow radio —
	both read a crew without that person and both append, and the refusal that
	exists by name for the sequential case ("Two rows for one person become two
	Attendance days when the shift closes") never runs.

	A SELECT ... FOR UPDATE ON THE ROW, WHICH IS THE WHOLE MECHANISM, and it is
	deliberately the one `tools/dispatch.py::lock_task` already takes for the
	claim race rather than a second pattern to reason about. Frappe wraps each
	request in one transaction, so the lock is held until that request commits.
	The second caller BLOCKS here instead of reading stale state, and when it
	wakes the crew says what the first caller left and it takes the ordinary
	refusal — or appends to the row set that actually exists.

	IT IS ONLY A LOCK IF BOTH SIDES TAKE IT, which is why the callers are the
	three tools that read the crew and then write it: `add_worker_to_shift`,
	`remove_worker_from_shift` and `end_shift`. The close is on the list because
	it is the same race with the worst outcome — a join landing between
	`is_open` and the close writes a crew row onto a shift whose Attendance has
	already been written, which is the state that tool's own refusal calls "a
	person with no payroll day".

	READ THE ROW AGAIN AFTER CALLING THIS. The lock makes a read authoritative;
	it does not refresh one already taken. Every caller re-resolves.
	"""
	if not name:
		return
	try:
		frappe.db.get_value(DOCTYPE, name, "name", for_update=True)
	except TypeError:  # pragma: no cover - a Frappe without for_update
		pass


def to_the_second(value) -> str:
	"""One timestamp as a string, with any sub-second part cut off.

	v0.96.0, AND IT IS A COMPARISON FIX RATHER THAN A STORAGE ONE — nothing that
	calls this rewrites a column. Every ordering check on a shift compares two
	timestamps as STRINGS, which is right for two values of the same width and
	wrong the moment the widths differ: `"2026-08-18 17:01:04"` sorts before
	`"2026-08-18 17:01:04.560880"` because it is a PREFIX of it, so the same
	instant reads as earlier than itself.

	That is not hypothetical. The handset formats every timestamp it sends as
	`yyyy-MM-dd HH:mm:ss` and Frappe stores `start_datetime` with microseconds,
	so a foreman who started a shift and immediately scanned their own badge onto
	it was told they had joined 0.56 of a second before the shift began — and the
	same rounding sits under the close, which refuses an `end_datetime` that
	precedes the start.

	TRUNCATION RATHER THAN A TOLERANCE WINDOW, deliberately. Every one of these
	guards exists to catch a transposition — a departure typed as an arrival, a
	date a day out — which is hours or days wrong, never fractions of a second.
	A window would be a number somebody has to defend; a shared resolution is
	just the two clocks agreeing on what a second is.

	A value with no sub-second part, and anything that is not a timestamp at all,
	comes back unchanged — so an unparseable column compares exactly as before.
	"""
	text = str(value or "").strip()
	if not text:
		return text
	head, dot, _tail = text.partition(".")
	return head if dot else text


#: Most crew rows one cross-shift check will walk for a single employee. A
#: picker's whole season is a few hundred rows and the query is filtered to one
#: employee, so this is a runaway guard rather than a real ceiling.
CREW_HISTORY_CAP = 1000


def open_shifts_for(employee: str, exclude: str = "") -> list:
	"""Every OPEN shift this employee is still standing on, `exclude` aside.

	THE QUESTION A SINGLE SHIFT CANNOT ANSWER ABOUT ITSELF. The Farm Shift
	controller refuses the same Employee twice on ONE crew, which stops the
	duplicate somebody can see — two rows side by side on one form. It says
	nothing about the same person being on two DIFFERENT open shifts, and that is
	the shape that actually happens: a foreman rosters Ana at six, the packing
	shed's lead rosters her again at ten on a shift of their own, and neither
	form shows the other. Both close, the Attendance bridge writes one row per
	crew row, and Ana is paid twice for one day out of records that each look
	correct on their own.

	STILL ON means two facts together: the crew row has no `left_at`, so nobody
	clocked them out of it, AND the shift itself has no `end_datetime`, so it is
	running. A worker whose morning shift was closed at noon is on nothing at
	one o'clock, and a `remove_worker_from_shift` at eleven ends their span
	whether or not the shift they were on carries on without them.

	Returns the shift rows rather than a bool, because a refusal that cannot
	name the other shift is one nobody can act on — the whole fix is "close
	SHIFT-2026-0114 first", and that needs the docname in the sentence.
	"""
	employee = str(employee or "").strip()
	if not employee or not compat.doctype_exists(CREW_DOCTYPE):
		return []
	rows = (
		frappe.db.get_all(
			CREW_DOCTYPE,
			filters={"employee": employee, "parenttype": DOCTYPE, "parentfield": "crew"},
			# `parent` IS NOT PASSED THROUGH `existing_fields`, because it is one of
			# Frappe's own standard columns on every child table rather than a field
			# the doctype declares — filtering it out would leave every row without
			# the one value this function is here to read.
			fields=["parent", *compat.existing_fields(CREW_DOCTYPE, ("joined_at", "left_at"))],
			limit=CREW_HISTORY_CAP,
		)
		or []
	)
	out = []
	seen = set()
	for row in rows:
		parent = str(row.get("parent") or "")
		if not parent or parent == str(exclude or "") or parent in seen:
			continue
		if str(row.get("left_at") or "").strip():
			continue
		shift = frappe.db.get_value(DOCTYPE, parent, list(FIELDS), as_dict=True)
		if not shift or not is_open(dict(shift)):
			continue
		seen.add(parent)
		entry = dict(shift)
		entry["joined_at"] = row.get("joined_at")
		out.append(entry)
	return sorted(out, key=lambda entry: str(entry.get("start_datetime") or ""))


#: Which day a week starts on, for the weekly ceiling in `minors.LIMITS`.
#: MONDAY, because ORS 653.010(12) defines a workweek as a fixed and regularly
#: recurring period of seven consecutive days and this app has to pick one — and
#: because a Sunday boundary would split a Saturday-Sunday harvest weekend across
#: two weeks, which is the direction that hides an over-hours week rather than
#: the one that shows it. An operation on a different workweek reads a figure
#: shifted by a day or two at the boundary; that is a known approximation and it
#: is stated in the refusal, rather than a silent claim of exactness.
WEEK_STARTS_MONDAY = True


def week_bounds(on_date: str) -> tuple:
	"""(first_day, last_day) of the workweek containing `on_date`, as YYYY-MM-DD."""
	day = str(on_date or "")[:10]
	try:
		parsed = _dt.datetime.strptime(day, "%Y-%m-%d").date()
	except ValueError:
		return ("", "")
	start = parsed - _dt.timedelta(days=parsed.weekday())
	return (start.isoformat(), (start + _dt.timedelta(days=6)).isoformat())


def hours_worked_by(employee: str, on_date: str, exclude: str = "", now: str = "") -> dict:
	"""How long this person has been on shifts today and this workweek.

	v0.98.0, AND IT READS THE SHIFTS RATHER THAN ATTENDANCE. `bridge_to_attendance`
	writes one Attendance row per crew member WHEN THE SHIFT CLOSES, so on the
	afternoon somebody is being rostered onto a second crew the Attendance
	register says nothing at all about the morning they have already worked —
	which is exactly the moment a child-labour hour check is being asked. The crew
	rows exist from the moment the shift is formed, so this reads those.

	AN OPEN SHIFT IS COUNTED TO `now`. A worker who started at six and is being
	added to something else at two has worked eight hours, and treating an
	unfinished span as zero would clear every over-hours case that is still
	happening. A CLOSED shift is counted to its own end.

	`exclude` drops one shift by docname — the one being rostered onto, so its own
	span is not counted twice by a caller that then adds it.

	Returns hours by day and for the workweek, with the shifts each came from, so
	a refusal can name them. Empty where the crew doctype is absent.
	"""
	out = {"today": 0.0, "week": 0.0, "day": str(on_date or "")[:10], "shifts": []}
	employee = str(employee or "").strip()
	if not employee or not compat.doctype_exists(CREW_DOCTYPE):
		return out
	first, last = week_bounds(on_date)
	if not first:
		return out
	out["week_start"], out["week_end"] = first, last
	stamp = str(now or "").strip() or frappe.utils.now()

	rows = (
		frappe.db.get_all(
			CREW_DOCTYPE,
			filters={"employee": employee, "parenttype": DOCTYPE, "parentfield": "crew"},
			fields=["parent", *compat.existing_fields(CREW_DOCTYPE, ("joined_at", "left_at"))],
			limit=CREW_HISTORY_CAP,
		)
		or []
	)
	seen = set()
	for row in rows:
		parent = str(row.get("parent") or "")
		if not parent or parent in seen or parent == str(exclude or ""):
			continue
		shift = frappe.db.get_value(DOCTYPE, parent, list(FIELDS), as_dict=True)
		if not shift:
			continue
		shift = dict(shift)
		if compat.checked(shift.get("cancelled")):
			# A shift that was called off is not hours anybody worked.
			continue
		seen.add(parent)
		start = str(row.get("joined_at") or shift.get("start_datetime") or "")
		day = start[:10]
		if not day or day < first or day > last:
			continue
		end = str(row.get("left_at") or shift.get("end_datetime") or "") or stamp
		hours = hours_between(start, end)
		if hours is None:
			continue
		out["week"] += hours
		if day == out["day"]:
			out["today"] += hours
		out["shifts"].append(
			{
				"shift": parent,
				"day": day,
				"joined_at": start,
				"until": end,
				"hours": hours,
				"open": is_open(shift),
			}
		)
	out["today"] = round(out["today"], 2)
	out["week"] = round(out["week"], 2)
	out["shifts"].sort(key=lambda entry: entry["joined_at"])
	return out


# ── reading ─────────────────────────────────────────────────────────────────
FIELDS = (
	"name",
	"foreman",
	"foreman_name",
	"company",
	"location",
	"farm_location_gps",
	"shift_type",
	"start_datetime",
	"end_datetime",
	"cancelled",
	"cancellation_reason",
	"status",
	"foreman_notes",
	"supervisor_review_signature",
	"supervisor_review_on",
	# v0.98.0. TWO COLUMNS THREE READERS ALREADY ASKED THIS TUPLE FOR AND NEVER
	# GOT. `_break_summary` right below, `_compute_shift_production` and
	# `get_shift_crew_timeline` all do `row.get("break_policy")` on a row built
	# from this list — so the lookup returned None on every shift ever, the
	# reconciliation was skipped, and `get_shift_crew_timeline` reported
	# `"break_policy": null` for shifts that named one. Nothing failed; a whole
	# block of entitlement figures was simply absent, which is the quietest way
	# for a compliance number to be missing.
	#
	# `work_state` JOINS IT because `get_break_schedule` falls back to the
	# enabled policy for the shift's state where the shift itself names none,
	# and that fallback has to read the state off the shift rather than off the
	# request — a phone that could name the state could ask for California's
	# rules on an Oregon crew.
	#
	# BOTH SHIP ON THIS APP'S OWN `farm_shift.json`, so `existing_fields` finds
	# them on any site the app is installed on; the one caller that passes this
	# tuple to Frappe unfiltered (`open_shifts_for`) is safe for the same reason.
	"work_state",
	"break_policy",
)

#: v0.64.0 added `pay_type` and `pay_rate`, which have been on the doctype since
#: the crew table shipped and were read nowhere. `get_shift_crew_timeline` is
#: what needed them: a worker's envelope decides what they are OWED as well as
#: what they were EXPOSED to, and a piece-rate picker who joined at ten and an
#: hourly foreman who was there from six are paid on different bases across the
#: same afternoon. Fetched through `compat.existing_fields`, so a site whose
#: migration has not reached the columns loses the two keys rather than the read.
CREW_FIELDS = (
	"name",
	"employee",
	"employee_name",
	"role",
	"joined_at",
	"left_at",
	"pay_type",
	"pay_rate",
	"notes",
	"idx",
)

#: v0.64.0 added the six break columns, which have been on the doctype since
#: v0.58.0 and were never FETCHED. Everything downstream reads these rows through
#: `events_of`, so their absence was not a missing key on a payload — it was
#: silent wrong arithmetic in three places at once:
#:
#:   * `describe_event_row`'s break branch is gated on `break_kind` and could
#:     never be entered, so no break event ever reported its kind or duration;
#:   * `breaks.worker_breaks` skips an Individual break that names somebody else
#:     — with `applies_to` absent it defaults to Crew, so ONE person's cool-down
#:     counted as a break taken by the whole crew;
#:   * the same function totals `duration_minutes`, which was absent and read as
#:     zero, so paid and unpaid break minutes were zero on every shift.
#:
#: Fetched through `compat.existing_fields`, so a site whose migration predates
#: the break columns loses the keys rather than the read.
EVENT_FIELDS = (
	"name",
	"event_type",
	"event_datetime",
	"logged_by",
	"description",
	"producer_record_doctype",
	"producer_record_name",
	"weather_snapshot_temp_f",
	"weather_snapshot_heat_index_f",
	"evidence_file",
	"break_kind",
	"ended_at",
	"duration_minutes",
	"duration_source",
	"applies_to",
	"employee",
	# v0.98.0. The heat block, added for the same reason the six break columns
	# were added in v0.64.0: a column that exists and is never FETCHED is a column
	# nothing downstream can read, and the failure is silent arithmetic rather
	# than a missing key. Through `compat.existing_fields`, so a site whose
	# migration has not reached them loses the keys rather than the read.
	"peak_temp_f",
	"peak_heat_index_f",
	"threshold_crossed_at",
	"weather_source",
	"heat_obligation",
	"idx",
)

WEATHER_FIELDS = (
	"name",
	"reading_datetime",
	"temp_f",
	"heat_index_f",
	"humidity_pct",
	"wind_speed_mph",
	"wind_direction_deg",
	"precipitation_mm",
	"source",
	"fetched_at",
	"idx",
)

LOCATION_FIELDS = (
	"name",
	"shift",
	"employee",
	"employee_name",
	"company",
	"timestamp",
	"source",
	"latitude",
	"longitude",
	"accuracy_meters",
	"h3_cell",
	"notes",
)

HEAT_FIELDS = (
	"name",
	"farm_shift",
	"foreman",
	"company",
	"event_date",
	"max_temp_f",
	"max_heat_index_f",
	"threshold_crossed_at",
	"water_provided",
	"shade_provided",
	"mandatory_rest_taken",
	"heat_illness_signs_observed",
	"worker_reported_symptoms",
	"emergency_response_activated",
	"training_verified",
	"supervisor_signature",
	"supervisor_signed_on",
	"regulation_citation",
	"notes",
	"docstatus",
)


def rows(filters: dict, limit: int = 2000, order_by: str = "start_datetime desc") -> list:
	"""Farm Shifts, selecting only the columns this site actually has."""
	if not compat.doctype_exists(DOCTYPE):
		return []
	return [
		dict(row)
		for row in frappe.db.get_all(
			DOCTYPE,
			filters=filters or {},
			fields=compat.existing_fields(DOCTYPE, FIELDS),
			order_by=order_by,
			limit=limit,
		)
		or []
	]


def heat_rows(filters: dict, limit: int = 2000, order_by: str = "event_date desc") -> list:
	if not compat.doctype_exists(HEAT_DOCTYPE):
		return []
	return [
		dict(row)
		for row in frappe.db.get_all(
			HEAT_DOCTYPE,
			filters=filters or {},
			fields=compat.existing_fields(HEAT_DOCTYPE, HEAT_FIELDS),
			order_by=order_by,
			limit=limit,
		)
		or []
	]


def _child_rows(doctype: str, fields, parent: str, parentfield: str, order_by: str) -> list:
	if not compat.doctype_exists(doctype):
		return []
	return [
		dict(row)
		for row in frappe.db.get_all(
			doctype,
			filters={"parent": parent, "parenttype": DOCTYPE, "parentfield": parentfield},
			fields=compat.existing_fields(doctype, fields),
			order_by=order_by,
			limit=2000,
		)
		or []
	]


def crew_of(shift: str) -> list:
	"""The crew rows of one shift, in the order the foreman built them."""
	return _child_rows(CREW_DOCTYPE, CREW_FIELDS, shift, "crew", "idx asc")


def minor_flags(employees, on_date: str = "") -> dict:
	"""employee docname → `minors.describe`, in ONE query for the whole crew.

	v0.98.0. ONE QUERY AND NOT ONE PER ROW: a thirty-person crew described inside
	an audit packet that walks a season of shifts is thirty thousand round trips
	the moment this is done per member, and the answer is a single column.

	`on_date` IS THE SHIFT'S OWN DAY AND NOT TODAY where the caller has one. A
	shift being read in November is being read about the afternoon it happened,
	and a picker who turned eighteen in September was seventeen on the day the
	crew was formed. Defaulting to today would rewrite last season's roster on
	every birthday.

	Empty where the Employee doctype is absent or has no `date_of_birth`, which
	leaves every `is_minor` at None: "this site does not record it" rather than
	"nobody is under eighteen".
	"""
	wanted = sorted({str(name) for name in (employees or []) if name})
	if not wanted:
		return {}
	if not compat.doctype_exists("Employee") or not compat.has_field("Employee", "date_of_birth"):
		return {}
	when = str(on_date or "").strip() or frappe.utils.today()
	rows = (
		frappe.db.get_all(
			"Employee",
			filters={"name": ("in", wanted)},
			fields=["name", "date_of_birth"],
			limit=len(wanted),
		)
		or []
	)
	born = {str(row["name"]): row.get("date_of_birth") for row in rows}
	return {name: minors_mod.describe(born.get(name), when) for name in wanted}


def events_of(shift: str) -> list:
	"""The compliance events of one shift, IN TIME ORDER.

	Ordered by when they happened rather than by the order somebody entered them,
	because the thing being read is a timeline and an event logged late in the
	afternoon about the morning belongs where it happened.
	"""
	return sorted(
		_child_rows(EVENT_DOCTYPE, EVENT_FIELDS, shift, "compliance_events", "idx asc"),
		key=lambda row: str(row.get("event_datetime") or ""),
	)


def weather_of(shift: str) -> list:
	"""The weather timeline of one shift. Empty in v0.19.3 — see the doctype."""
	return sorted(
		_child_rows(WEATHER_DOCTYPE, WEATHER_FIELDS, shift, "weather_timeline", "idx asc"),
		key=lambda row: str(row.get("reading_datetime") or ""),
	)


def track_of(shift: str, employee: str = "", limit: int = 5000) -> list:
	"""One shift's GPS breadcrumbs, IN TIME ORDER.

	v0.32.0. Ordered by when the fix was TAKEN rather than by when it landed,
	which is the whole reason the doctype carries both ideas. A phone out of
	signal in a canyon posts an hour of breadcrumbs the moment the bars come back,
	so a track sorted by insertion draws the crew standing still all morning at the
	spot where the signal returned and then teleporting.
	"""
	if not compat.doctype_exists(LOCATION_DOCTYPE):
		return []
	filters = {"shift": shift}
	if employee:
		filters["employee"] = employee
	return [
		dict(row)
		for row in frappe.db.get_all(
			LOCATION_DOCTYPE,
			filters=filters,
			fields=compat.existing_fields(LOCATION_DOCTYPE, LOCATION_FIELDS),
			order_by="timestamp asc",
			limit=limit,
		)
		or []
	]


def describe_location_row(row: dict) -> dict:
	"""One breadcrumb, in the shape every tool and map reports it."""
	accuracy = row.get("accuracy_meters")
	return {
		"name": row.get("name"),
		"employee": row.get("employee") or None,
		"employee_name": row.get("employee_name") or row.get("employee") or None,
		"timestamp": str(row.get("timestamp") or "") or None,
		"lat": round(float(row.get("latitude") or 0), 7),
		"lon": round(float(row.get("longitude") or 0), 7),
		"accuracy_meters": round(float(accuracy), 2) if accuracy not in (None, "") else None,
		"h3_cell": row.get("h3_cell") or None,
		"source": row.get("source") or None,
		"notes": row.get("notes") or None,
	}


def acclimatization_of(heat_event: str) -> list:
	"""The Employee docnames on one heat record's acclimatization plan."""
	if not compat.doctype_exists(ACCLIMATIZATION_DOCTYPE):
		return []
	return [
		str(row.get("employee"))
		for row in frappe.db.get_all(
			ACCLIMATIZATION_DOCTYPE,
			filters={"parent": heat_event, "parenttype": HEAT_DOCTYPE},
			fields=["employee", "idx"],
			order_by="idx asc",
			limit=2000,
		)
		or []
		if row.get("employee")
	]


# ── describing ──────────────────────────────────────────────────────────────
def describe_crew_row(row: dict, shift_end: str = "") -> dict:
	"""One crew member, with the span the Attendance bridge will use.

	`present_until` is the honest reading of an empty `left_at`: they were there
	to the end. It is COMPUTED and reported rather than written back onto the row,
	because writing it would destroy the distinction between "left at 13:00" and
	"stayed to the end" the moment the end time ever changed.
	"""
	left = str(row.get("left_at") or "") or None
	return {
		"employee": row.get("employee"),
		"employee_name": row.get("employee_name"),
		"role": row.get("role") or "Worker",
		"joined_at": str(row.get("joined_at") or "") or None,
		"left_at": left,
		"present_until": left or (str(shift_end or "") or None),
		"left_early": bool(left),
		"notes": row.get("notes") or None,
		# v0.98.0. DERIVED UPSTREAM AND MERELY CARRIED HERE — this function is
		# pure over the row it is given, and `describe` is what runs the one
		# query. None where the row was not enriched or no date of birth is on
		# file; the three-valued answer is the point (see `minors.py`).
		"is_minor": row.get("is_minor"),
		"minor_band": row.get("minor_band"),
		"minor_limits": row.get("minor_limits"),
	}


def _float(value, default: float = 0.0) -> float:
	try:
		return float(value) if value not in (None, "") else default
	except (TypeError, ValueError):
		return default


def heat_conditions(readings: list, when: str) -> dict:
	"""What this shift's weather had done BY `when` — the peak, and the crossing.

	v0.98.0, AND IT IS A DIFFERENT QUESTION FROM THE SNAPSHOT the Farm Shift
	controller already writes onto every event. `weather_snapshot_temp_f` is the
	reading current at the event's own instant, bounded to half an hour; that is
	the conditions the foreman was standing in. This is the shift's HIGHEST
	figures so far and the moment the heat index first crossed the threshold — the
	conditions the break was called ABOUT.

	The distinction is not academic. A cool-down called at 16:10, when the index
	has fallen back to 88, sits on a row whose snapshot reads 88 and whose peak
	reads 97. OAR 437-004-1131 attaches its obligations at the CROSSING, and an
	inspector asking whether relief was provided in time is asking about the
	crossing rather than about the moment of relief. A register that carried only
	the snapshot could not answer, and the join to reconstruct it — ninety
	readings, per break, per shift — is the one nobody performs.

	AT OR BEFORE `when`, never after: reaching forward would stamp a break with a
	measurement that did not exist when it was called. Empty where the shift has
	no timeline, which is not the same as zero and is why the caller writes
	nothing rather than writing nulls.
	"""
	if not when:
		return {}
	before = [
		row
		for row in (readings or [])
		if str(row.get("reading_datetime") or "") and str(row["reading_datetime"]) <= str(when)
	]
	if not before:
		return {}
	temps = [row.get("temp_f") for row in before if row.get("temp_f") not in (None, "")]
	indices = [row.get("heat_index_f") for row in before if row.get("heat_index_f") not in (None, "")]
	crossed = next(
		(
			str(row["reading_datetime"])
			for row in before
			if _float(row.get("heat_index_f")) >= HEAT_THRESHOLD_F
		),
		"",
	)
	out = {
		"peak_temp_f": max(temps) if temps else None,
		"peak_heat_index_f": max(indices) if indices else None,
		"threshold_crossed_at": crossed or None,
		# The SOURCE OF THE READING CURRENT AT THE BREAK, not of the peak. The
		# provenance question a reader asks is "where did the numbers on this row
		# come from", and the row's own snapshot is the number they are looking
		# at when they ask.
		"weather_source": str(before[-1].get("source") or "") or None,
	}
	return out


def describe_event_row(row: dict) -> dict:
	producer = str(row.get("producer_record_doctype") or "").strip()
	name = str(row.get("producer_record_name") or "").strip()
	out = {
		"event_type": row.get("event_type"),
		"event_datetime": str(row.get("event_datetime") or "") or None,
		"logged_by": row.get("logged_by") or None,
		"description": row.get("description") or None,
		"producer_record": f"{producer} {name}".strip() or None,
		"producer_record_doctype": producer or None,
		"producer_record_name": name or None,
		"temp_f": row.get("weather_snapshot_temp_f"),
		# v0.98.0. THE SAME NUMBER UNDER THE NAME iOS ASKS FOR. `temp_f` has been
		# this key since v0.19.4 and is not going anywhere — every existing reader
		# uses it — but the handset's heat-break payload calls it `ambient_temp_f`,
		# and a server answering with one name while the client reads the other is
		# the exact class of failure v0.96.0 was seven instances of. Both, from
		# one column, so they cannot drift.
		"ambient_temp_f": row.get("weather_snapshot_temp_f"),
		"heat_index_f": row.get("weather_snapshot_heat_index_f"),
		"weather_source": row.get("weather_source") or None,
		"evidence_attached": bool(row.get("evidence_file")),
	}
	break_kind = row.get("break_kind") or ""
	if break_kind:
		out["break_kind"] = break_kind
		out["ended_at"] = str(row.get("ended_at") or "") or None
		out["duration_minutes"] = row.get("duration_minutes")
		out["duration_source"] = row.get("duration_source") or "Scheduled"
		out["applies_to"] = row.get("applies_to") or "Crew"
		out["employee"] = row.get("employee") or None
		out["heat_obligation"] = compat.checked(row.get("heat_obligation"))
		if out["heat_obligation"]:
			out["peak_temp_f"] = row.get("peak_temp_f")
			out["peak_heat_index_f"] = row.get("peak_heat_index_f")
			out["threshold_crossed_at"] = str(row.get("threshold_crossed_at") or "") or None
	return out


def describe(row: dict, with_children: bool = False, clock=None) -> dict:
	"""One shift in the shape every tool and packet reports it.

	`clock` is a `timezones.Renderer`. Passed in by a caller describing a list of
	shifts so the site's zone is read once; omitted, the stored naive timestamps
	come back on their own exactly as they always have. NOT constructed here when
	absent, unlike `shape.task`: this describer runs inside audit packets and
	report generators where nothing wants the extra keys, and a shift's start
	time is the one timestamp on this farm that a payroll figure is computed
	from — quietly adding columns to it is not free.
	"""
	name = str(row.get("name") or "")
	end = str(row.get("end_datetime") or "") or None
	out = {
		"name": name,
		"foreman": row.get("foreman"),
		"foreman_name": row.get("foreman_name"),
		"company": row.get("company"),
		"location": row.get("location") or None,
		"farm_location_gps": row.get("farm_location_gps") or None,
		"shift_type": row.get("shift_type") or None,
		"start_datetime": str(row.get("start_datetime") or "") or None,
		"end_datetime": end,
		"open": end is None,
		"status": status_for(end, row.get("cancelled")),
		"cancelled": compat.checked(row.get("cancelled")),
		"cancellation_reason": row.get("cancellation_reason") or None,
		"supervisor_reviewed": bool(row.get("supervisor_review_signature")),
		"supervisor_review_on": str(row.get("supervisor_review_on") or "") or None,
		"foreman_notes": row.get("foreman_notes") or None,
	}
	if clock is not None:
		# Which six o'clock the crew started. A shift that began at 05:30 and a
		# server reporting 12:30 are the same instant and only one of them is the
		# morning anybody turned up for.
		clock.add(out, "start_datetime", "end_datetime", "supervisor_review_on")
	if not with_children or not name:
		return out
	crew = crew_of(name)
	events = events_of(name)
	readings = weather_of(name)
	# v0.98.0. WHO ON THIS CREW IS UNDER EIGHTEEN, resolved once for the whole
	# roster and answered as of the SHIFT'S OWN DAY. The described rows carry it
	# so the handset can put the purple "Minor's schedule" badge on the crew
	# screen, and `_break_summary` carries it so their entitlement is computed
	# from the minor schedule rather than the adult one — two readers, one query,
	# one answer they cannot disagree about.
	flags = minor_flags([entry.get("employee") for entry in crew], str(row.get("start_datetime") or ""))
	for entry in crew:
		entry.update(flags.get(str(entry.get("employee") or "")) or {})
	out["crew"] = [describe_crew_row(entry, end or "") for entry in crew]
	out["crew_size"] = len(crew)
	out["still_on_shift"] = len([entry for entry in crew if not entry.get("left_at")])
	out["minors_on_crew"] = len([entry for entry in crew if entry.get("is_minor")])
	out["compliance_events"] = [describe_event_row(entry) for entry in events]
	out["compliance_event_count"] = len(events)
	out["weather_timeline"] = readings
	out["weather_reading_count"] = len(readings)
	out["break_summary"] = _break_summary(row, crew, events)
	return out


def _break_summary(shift_row: dict, crew: list, events: list) -> dict | None:
	"""Break reconciliation for the shift, if a break policy is linked."""
	policy_name = shift_row.get("break_policy")
	if not policy_name:
		return None
	try:
		pdoc = frappe.get_doc("Labor Break Policy", policy_name)
	except Exception:
		return None
	policy = pdoc.as_dict()
	break_events = [e for e in events if e.get("break_kind")]
	shift_dict = {
		"start_datetime": shift_row.get("start_datetime"),
		"end_datetime": shift_row.get("end_datetime"),
	}
	recon = breaks_mod.crew_reconciliation(shift_dict, crew, break_events, policy)
	rest_logged = sum(1 for e in break_events if e.get("break_kind") == "Paid Rest")
	meal_logged = sum(1 for e in break_events if e.get("break_kind") == "Unpaid Meal")
	cool_down_logged = sum(1 for e in break_events if e.get("break_kind") == "Cool-Down")
	# v0.96.0. THE TWO HEAT PROVISIONS THAT ARE ASKED ABOUT BY NAME. `break_kind`
	# gained Water Break and Shade Break in this release, and counting them into
	# `cool_down_logged` would have kept the summary's arithmetic right and
	# thrown away the only thing the new values were added for: OAR
	# 437-004-1131 asks whether SHADE was provided, and "three cool-downs" is
	# not an answer to it. `cool_down_logged` still counts Cool-Down alone, so
	# nothing already reading it changed meaning.
	water_break_logged = sum(1 for e in break_events if e.get("break_kind") == "Water Break")
	shade_break_logged = sum(1 for e in break_events if e.get("break_kind") == "Shade Break")
	scheduled_not_observed = [
		{
			"name": e.get("name"),
			"break_kind": e.get("break_kind"),
			"event_datetime": str(e.get("event_datetime") or ""),
			"duration_minutes": e.get("duration_minutes"),
		}
		for e in break_events
		if (e.get("duration_source") or "Scheduled") == "Scheduled" and not e.get("ended_at")
	]
	return {
		"policy": policy_name,
		"work_state": policy.get("work_state"),
		"approved": bool(policy.get("human_approved_by")),
		"crew_totals": {
			"rest_logged": rest_logged,
			"meal_logged": meal_logged,
			"cool_down_logged": cool_down_logged,
			"water_break_logged": water_break_logged,
			"shade_break_logged": shade_break_logged,
			# The cool-down CYCLE all three discharge, as one number — the clock
			# `breaks.next_break_due` counts from. See `breaks.HEAT_RELIEF_KINDS`.
			"heat_relief_logged": cool_down_logged + water_break_logged + shade_break_logged,
		},
		"workers_short": recon.get("workers_short", []),
		"breaks_scheduled_not_observed": scheduled_not_observed,
	}


def describe_heat_event(row: dict, with_plan: bool = True) -> dict:
	name = str(row.get("name") or "")
	out = {
		"name": name,
		"farm_shift": row.get("farm_shift"),
		"foreman": row.get("foreman"),
		"company": row.get("company"),
		"event_date": str(row.get("event_date") or "") or None,
		"max_temp_f": row.get("max_temp_f"),
		"max_heat_index_f": row.get("max_heat_index_f"),
		"threshold_crossed_at": str(row.get("threshold_crossed_at") or "") or None,
		"water_provided": compat.checked(row.get("water_provided")),
		"shade_provided": compat.checked(row.get("shade_provided")),
		"mandatory_rest_taken": compat.checked(row.get("mandatory_rest_taken")),
		"heat_illness_signs_observed": compat.checked(row.get("heat_illness_signs_observed")),
		"worker_reported_symptoms": compat.checked(row.get("worker_reported_symptoms")),
		"emergency_response_activated": compat.checked(row.get("emergency_response_activated")),
		"training_verified": compat.checked(row.get("training_verified")),
		"supervisor_signed": bool(row.get("supervisor_signature")),
		"supervisor_signed_on": str(row.get("supervisor_signed_on") or "") or None,
		"regulation_citation": row.get("regulation_citation") or CITATION,
		"submitted": int(row.get("docstatus") or 0) == 1,
		"notes": row.get("notes") or None,
	}
	if with_plan and name:
		plan = acclimatization_of(name)
		out["acclimatization_plan"] = plan
		out["unacclimatized_workers"] = len(plan)
	return out


def heat_gaps(described: dict) -> list:
	"""Which -1131 obligations this record does NOT claim were met.

	REPORTED, NEVER REFUSED — the same posture `training.fsma_161_gaps` takes and
	for the same reason. A shift where the shade trailer broke down and the crew
	was sent home at eleven is a shift with a real gap, and a system that would not
	let the gap be recorded would produce either a false record or no record. The
	honest one is worth more under investigation than a row of ticks, and it is
	also the one that tells somebody what to fix.
	"""
	gaps = []
	if not described.get("water_provided"):
		gaps.append(
			f"{CITATION}(f) — the record does not claim drinking water was provided at the "
			"required rate. This is the first thing an inspector asks about and the first "
			"thing a crew remembers"
		)
	if not described.get("shade_provided"):
		gaps.append(f"{CITATION}(e) — the record does not claim shade was available and accessible")
	if not described.get("mandatory_rest_taken"):
		gaps.append(
			f"{CITATION}(h) — the record does not claim the preventative cool-down rest cycle "
			"was taken. Offered is not taken, and a crew on a piece rate declining a break is "
			"the failure the requirement exists for"
		)
	if not described.get("heat_illness_signs_observed") and not described.get("worker_reported_symptoms"):
		# NOT a gap in itself — a shift where nobody showed signs is the ordinary
		# and desirable case. It is reported only so a reader knows the two
		# observation columns were answered rather than skipped.
		pass
	if not described.get("training_verified"):
		gaps.append(
			f"{CITATION}(i) — the record does not confirm every worker on the crew had current "
			"heat illness prevention training. The rule requires it annually AND before work at "
			"a site where the heat index will reach 80 °F, so a gap here is a citation on the "
			"first hot morning rather than at the next inspection"
		)
	if not described.get("supervisor_signed"):
		gaps.append(
			"FSMA §112.161(b) / -1131 supervision — no supervisor signature is attached, so "
			"nobody's name is against these claims"
		)
	return gaps


# ── the Attendance bridge ───────────────────────────────────────────────────
def attendance_span(crew_row: dict, shift_end: str) -> tuple:
	"""(in_time, out_time) for one crew member on a closing shift.

	Their own joined_at to their own left_at, falling back to the shift's end
	where they stayed to it. NOT the shift's own span: a worker who arrived an
	hour late and left two hours early worked six hours of a nine-hour shift, and
	an Attendance row claiming nine is a wage record that is wrong in the
	employer's favour — which is the direction that gets litigated.
	"""
	joined = str(crew_row.get("joined_at") or "").strip()
	left = str(crew_row.get("left_at") or "").strip() or str(shift_end or "").strip()
	return (joined or None, left or None)


def hours_between(start: str, end: str):
	if not start or not end:
		return None
	try:
		seconds = float(frappe.utils.time_diff_in_seconds(end, start))
	except Exception:
		return None
	if seconds < 0:
		return None
	return round(seconds / 3600.0, 2)


def bridge_to_attendance(shift: dict, crew: list) -> dict:
	"""One submitted Attendance per crew member for a shift that has just closed.

	NEVER RAISES. See the module docstring: the supervisor's signature is the
	compliance act and the payroll row is the convenience, so a site without
	Frappe HR, an employee archived since the shift ran, and a day somebody
	already keyed in by hand all produce a REPORTED skip rather than a refusal to
	close a signed shift.

	A row already carrying this shift is left exactly as it is, so closing an
	amended shift twice does not double somebody's day.
	"""
	report = {"created": [], "skipped": [], "failed": [], "available": True}
	if not compat.doctype_exists(ATTENDANCE_DOCTYPE):
		report["available"] = False
		report["note"] = (
			"This site has no Attendance DocType, so no payroll rows were written. The shift is "
			"closed and its crew spans are on the shift itself — install Frappe HR and the bridge "
			"works on the next close. Nothing about the compliance record depends on it."
		)
		return report

	has_shift_field = compat.has_field(ATTENDANCE_DOCTYPE, ATTENDANCE_SHIFT_FIELD)
	end = str(shift.get("end_datetime") or "")
	date = end[:10] or str(shift.get("start_datetime") or "")[:10]

	for row in crew or []:
		employee = str(row.get("employee") or "")
		if not employee:
			continue
		joined, left = attendance_span(row, end)
		try:
			if has_shift_field and frappe.db.exists(
				ATTENDANCE_DOCTYPE,
				{ATTENDANCE_SHIFT_FIELD: shift.get("name"), "employee": employee},
			):
				report["skipped"].append(
					{
						"employee": employee,
						"reason": "an Attendance row for this employee already names this shift",
					}
				)
				continue
			doc = frappe.new_doc(ATTENDANCE_DOCTYPE)
			doc.employee = employee
			if compat.has_field(ATTENDANCE_DOCTYPE, "employee_name"):
				doc.employee_name = row.get("employee_name") or employee
			doc.attendance_date = str(joined or "")[:10] or date
			doc.status = ATTENDANCE_STATUS
			if compat.has_field(ATTENDANCE_DOCTYPE, "company"):
				doc.company = shift.get("company")
			if compat.has_field(ATTENDANCE_DOCTYPE, "in_time"):
				doc.in_time = joined
			if compat.has_field(ATTENDANCE_DOCTYPE, "out_time"):
				doc.out_time = left
			hours = hours_between(joined or "", left or "")
			if hours is not None and compat.has_field(ATTENDANCE_DOCTYPE, "working_hours"):
				doc.working_hours = hours
			if has_shift_field:
				doc.set(ATTENDANCE_SHIFT_FIELD, shift.get("name"))
			doc.flags.ignore_permissions = True
			doc.insert(ignore_permissions=True)
			# SUBMITTED, because `get_attendance_summary` counts docstatus 1 only —
			# a draft row is not a fact about whether somebody turned up, and a
			# bridge that left drafts would produce a payroll register that looked
			# empty for every shift-formed day.
			doc.submit()
			report["created"].append(
				{
					"attendance": doc.name,
					"employee": employee,
					"employee_name": row.get("employee_name") or employee,
					"in_time": joined,
					"out_time": left,
					"hours": hours,
				}
			)
		except Exception as exc:  # pragma: no cover - a half-migrated HR app
			report["failed"].append({"employee": employee, "error": f"{type(exc).__name__}: {exc}"})

	if not has_shift_field:
		report["link_note"] = (
			f"Attendance has no `{ATTENDANCE_SHIFT_FIELD}` column on this site, so the rows written "
			"do not point back at the shift. It is a Custom Field this app installs — run `bench "
			"--site <site> migrate`, or tick Install Compliance Fields on ERPNext MCP Settings if "
			"somebody turned it off. Until then the bridge cannot tell its own rows from a "
			"hand-keyed day, so re-closing an amended shift would duplicate them."
		)
	if report["failed"]:
		report["failure_note"] = (
			f"{len(report['failed'])} Attendance row(s) could not be written and the shift is "
			"closed anyway. The supervisor's signature is the compliance act and the payroll row "
			"is the convenience — refusing the close would have lost the first to save the second. "
			"The spans are on the shift's own crew table either way."
		)
	return report

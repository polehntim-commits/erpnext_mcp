# SPDX-License-Identifier: MIT
"""Break entitlement and reconciliation — pure functions, no database.

Same contract as `bucket_bridge.py`, `payroll_integration.py` and `payroll_gl.py`:
everything arrives as a dict, nothing writes, nothing imports `frappe`.

The policy is a `Labor Break Policy` record read into a dict before it reaches
here. The events are `Farm Shift Compliance Event` rows — each one with at
least `event_type`, `event_datetime`, `break_kind`, `ended_at`,
`duration_minutes`, `applies_to` and `employee`.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

# ── helpers ───────────────────────────────────────────────────────────────────


def _as_float(value, default: float = 0.0) -> float:
	try:
		return float(value) if value not in (None, "") else default
	except (TypeError, ValueError):
		return default


def _parse_dt(value) -> _dt.datetime | None:
	if isinstance(value, _dt.datetime):
		return value
	if not value:
		return None
	s = str(value).strip()
	for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
		try:
			return _dt.datetime.strptime(s, fmt)
		except ValueError:
			continue
	return None


def _hours_between(start, end) -> float:
	a, b = _parse_dt(start), _parse_dt(end)
	if a is None or b is None:
		return 0.0
	return max((b - a).total_seconds() / 3600, 0.0)


#: The break kinds that discharge a HEAT obligation rather than a wage-and-hour
#: one. v0.96.0 — `Water Break` and `Shade Break` joined `Cool-Down` on the
#: `break_kind` Select in the same release, and every place that used to test
#: `== "Cool-Down"` tests membership here instead.
#:
#: THE THREE ARE ONE COUNTER HERE AND THREE RECORDS ON THE TIMELINE, which is
#: the split the regulation asks for. OAR 437-004-1131 and WAC 296-307-097 make
#: shade, drinking water and cool-down separately REQUIRED — so the log has to
#: keep them apart — while the cool-down CYCLE they satisfy is one clock: a crew
#: sent to the shade at the top of the hour has had its relief for that hour
#: whichever of the three words the foreman tapped. Counting them separately
#: would tell a crew that had just come out of the shade that a cool-down was
#: overdue.
HEAT_RELIEF_KINDS = ("Cool-Down", "Water Break", "Shade Break")


def _schedule_rows(policy: dict, key: str) -> list[dict]:
	return policy.get(key) or []


#: The adult schedule table, and the one that supersedes it for a worker under
#: eighteen. v0.98.0.
#:
#: A FALLBACK RATHER THAN A REQUIREMENT, and the direction of the fallback is
#: the whole of the design. A policy with no minor rows computes a minor's
#: entitlement from the ADULT schedule — which owes FEWER periods, and so shows
#: up as a shortfall the moment anybody looks rather than as silent generosity.
#: The opposite fallback ("no minor rows, no extra obligation") would report a
#: fifteen-year-old as fully rested on a policy nobody had finished filling in.
#:
#: OAR 839-021-0072 is why the rows differ at all: a minor is owed a rest period
#: a 15-minute rest (10 for an adult) at the same frequency as an adult under OAR 839-020-0050 —
#: corrected in v0.302.0 (BOLI). v0.302.0: `install.py` seeds a DISABLED, UNAPPROVED Oregon
#: policy carrying both bands from BOLI's chart, for a person to check and approve.
MINOR_SCHEDULE_KEYS = {
	"rest_schedule": "minor_rest_schedule",
	"meal_schedule": "minor_meal_schedule",
}


def _schedule_key(key: str, is_minor) -> str:
	"""Which table this worker's entitlement is read from.

	`is_minor` is None on a worker with no date of birth on file, and None is
	falsy here — the adult schedule. Deliberate: this decides an entitlement, and
	inventing a minor's schedule for somebody whose age nobody recorded would
	report a shortfall that may not exist. The GAP is reported where it can still
	be acted on, by `add_worker_to_shift`.
	"""
	if not is_minor:
		return key
	return MINOR_SCHEDULE_KEYS.get(key, key)


def _checked(value) -> bool:
	return value in (1, "1", True)


# ── 1. entitlement ──────────────────────────────────────────────────────────


def entitlement(hours_worked: float, policy: dict, is_minor=False) -> dict:
	"""How many rest and meal periods are owed for this many hours.

	Walks the schedule rows and picks the band whose range contains
	`hours_worked`. Returns the HIGHEST matching band — a 10-hour shift owes
	everything a 6-hour shift owes plus more.

	`is_minor` reads the minor tables where the policy carries them and falls
	back to the adult ones where it does not — see `MINOR_SCHEDULE_KEYS`. The
	answer gains `schedule`, naming which band produced it: "two rests owed"
	means different things under the two, and a reader has no other way to tell.
	"""
	minor_rest = _schedule_rows(policy, _schedule_key("rest_schedule", is_minor))
	minor_meal = _schedule_rows(policy, _schedule_key("meal_schedule", is_minor))
	rest = _best_band(hours_worked, minor_rest or _schedule_rows(policy, "rest_schedule"))
	meal = _best_band(hours_worked, minor_meal or _schedule_rows(policy, "meal_schedule"))
	return {
		"rest_periods": rest.get("periods_owed", 0) if rest else 0,
		"rest_minutes": (rest["periods_owed"] * rest["minutes_each"]) if rest else 0,
		"meal_periods": meal.get("periods_owed", 0) if meal else 0,
		"meal_minutes": (meal["periods_owed"] * meal["minutes_each"]) if meal else 0,
		"schedule": "minor" if is_minor and (minor_rest or minor_meal) else "adult",
	}


def _best_band(hours: float, rows: list[dict]) -> dict | None:
	best = None
	for row in rows:
		lo = _as_float(row.get("hours_from"))
		hi = _as_float(row.get("hours_to"))
		if lo <= hours <= hi:
			if best is None or _as_float(row.get("periods_owed")) > _as_float(best.get("periods_owed")):
				best = row
		elif lo <= hours and hi < hours:
			if best is None or _as_float(row.get("periods_owed")) > _as_float(best.get("periods_owed")):
				best = row
	return best


# ── 2. heat_entitlement ─────────────────────────────────────────────────────


def heat_entitlement(hours_worked: float, weather_timeline: list[dict], policy: dict) -> dict:
	"""How many heat cool-down periods are owed given the weather timeline.

	Walks the heat schedule rows and finds the highest heat index in the
	timeline. Returns the cool-down obligation for that band.
	"""
	if not weather_timeline:
		return {"cool_down_periods": 0, "cool_down_minutes": 0, "concurrent_with_rest": False}

	max_hi = max(_as_float(r.get("heat_index_f")) for r in weather_timeline)
	heat_rows = _schedule_rows(policy, "heat_schedule")
	best = None
	for row in heat_rows:
		lo = _as_float(row.get("heat_index_from"))
		hi = _as_float(row.get("heat_index_to"))
		if lo <= max_hi <= hi:
			best = row
		elif lo <= max_hi and hi < max_hi:
			if best is None or _as_float(row.get("minutes_each")) > _as_float(
				(best or {}).get("minutes_each")
			):
				best = row

	if not best:
		return {"cool_down_periods": 0, "cool_down_minutes": 0, "concurrent_with_rest": False}

	every = _as_float(best.get("every_hours"), 1.0)
	periods = max(int(hours_worked / every), 0) if every > 0 else 0
	return {
		"cool_down_periods": periods,
		"cool_down_minutes": periods * int(_as_float(best.get("minutes_each"))),
		"concurrent_with_rest": _checked(best.get("concurrent_with_rest")),
		"heat_index": max_hi,
	}


# ── 3. overlap_minutes ─────────────────────────────────────────────────────


def overlap_minutes(event: dict, segment_start, segment_end) -> float:
	"""Minutes of overlap between a break event's window and a worker's segment.

	The event window is [event_datetime, event_datetime + duration_minutes].
	The segment is [segment_start, segment_end].
	"""
	ev_start = _parse_dt(event.get("event_datetime"))
	dur = _as_float(event.get("duration_minutes"))
	if ev_start is None or dur <= 0:
		return 0.0
	ev_end_dt = _parse_dt(event.get("ended_at"))
	if ev_end_dt is not None:
		ev_end = ev_end_dt
	else:
		ev_end = ev_start + _dt.timedelta(minutes=dur)

	seg_s = _parse_dt(segment_start)
	seg_e = _parse_dt(segment_end)
	if seg_s is None or seg_e is None:
		return 0.0

	overlap_start = max(ev_start, seg_s)
	overlap_end = min(ev_end, seg_e)
	if overlap_start >= overlap_end:
		return 0.0
	return (overlap_end - overlap_start).total_seconds() / 60.0


# ── 4. worker_breaks ────────────────────────────────────────────────────────


def worker_breaks(segment: dict, events: list[dict], policy: dict) -> dict:
	"""Break hours for one worker on one shift segment.

	`segment` has `joined_at` and `left_at` (or the shift's start/end).
	`events` are all compliance events on the shift with a `break_kind` set.
	`policy` is the Labor Break Policy dict.

	Returns paid_break_hours, unpaid_break_hours, rest/meal taken/owed, shortfall.
	"""
	seg_start = segment.get("joined_at") or segment.get("start_datetime")
	seg_end = segment.get("left_at") or segment.get("end_datetime")
	hours_worked = _hours_between(seg_start, seg_end)

	ent = entitlement(hours_worked, policy, segment.get("is_minor"))

	paid_minutes = 0.0
	unpaid_minutes = 0.0
	rest_taken = 0
	meal_taken = 0
	cool_down_minutes = 0.0

	employee = segment.get("employee")

	for ev in events:
		kind = ev.get("break_kind") or ""
		if not kind:
			continue
		applies = ev.get("applies_to") or "Crew"
		if applies == "Individual" and ev.get("employee") != employee:
			continue

		mins = overlap_minutes(ev, seg_start, seg_end)
		if mins <= 0:
			continue

		if kind == "Paid Rest":
			paid_minutes += mins
			rest_taken += 1
		elif kind == "Unpaid Meal":
			unpaid_minutes += mins
			meal_taken += 1
		elif kind in HEAT_RELIEF_KINDS:
			cool_down_minutes += mins

	# Cool-down concurrent logic: if the policy says cool-downs run concurrently
	# with rest, the cool-down minutes do NOT add to paid_minutes when a rest
	# period also covers that time. We count them as paid rest only when they
	# are NOT concurrent, or when there aren't enough rest periods to absorb them.
	heat_ent = heat_entitlement(hours_worked, segment.get("weather_timeline") or [], policy)
	if heat_ent.get("concurrent_with_rest") and rest_taken > 0:
		# Cool-downs absorbed by rest periods — already counted as Paid Rest
		pass
	else:
		paid_minutes += cool_down_minutes

	paid_break_hours = round(paid_minutes / 60, 4)
	unpaid_break_hours = round(unpaid_minutes / 60, 4)

	rest_owed = ent["rest_periods"]
	meal_owed = ent["meal_periods"]
	rest_short = max(rest_owed - rest_taken, 0)
	meal_short = max(meal_owed - meal_taken, 0)
	shortfall_minutes = rest_short * (ent["rest_minutes"] / max(rest_owed, 1)) + meal_short * (
		ent["meal_minutes"] / max(meal_owed, 1)
	)

	return {
		"paid_break_hours": paid_break_hours,
		"unpaid_break_hours": unpaid_break_hours,
		"rest_taken": rest_taken,
		"rest_owed": rest_owed,
		"meal_taken": meal_taken,
		"meal_owed": meal_owed,
		"shortfall_minutes": round(shortfall_minutes, 1),
		"schedule": ent["schedule"],
	}


# ── 5. crew_reconciliation ─────────────────────────────────────────────────


def crew_reconciliation(shift: dict, crew: list[dict], events: list[dict], policy: dict) -> dict:
	"""Which workers on this shift are short of their break entitlement."""
	break_events = [ev for ev in events if ev.get("break_kind")]
	workers_short = []
	for member in crew:
		seg = {
			"employee": member.get("employee"),
			"joined_at": member.get("joined_at") or shift.get("start_datetime"),
			"left_at": member.get("left_at") or shift.get("end_datetime"),
			# v0.98.0. Set by `shifts.describe` from one query over the whole
			# crew. Absent on a caller that assembled the rows by hand, which
			# reads as an adult — the conservative direction, because the adult
			# schedule owes fewer periods and so cannot hide a shortfall.
			"is_minor": member.get("is_minor"),
		}
		wb = worker_breaks(seg, break_events, policy)
		if wb["rest_taken"] < wb["rest_owed"] or wb["meal_taken"] < wb["meal_owed"]:
			workers_short.append(
				{
					"employee": member.get("employee"),
					"employee_name": member.get("employee_name") or member.get("employee"),
					"rest_owed": wb["rest_owed"],
					"rest_taken": wb["rest_taken"],
					"meal_owed": wb["meal_owed"],
					"meal_taken": wb["meal_taken"],
					"shortfall_minutes": wb["shortfall_minutes"],
					"schedule": wb["schedule"],
					"is_minor": bool(member.get("is_minor")),
				}
			)
	return {
		"workers_short": workers_short,
		"minors_on_crew": len([member for member in crew if member.get("is_minor")]),
	}


# ── 6. next_break_due ──────────────────────────────────────────────────────


def next_break_due(
	now: Any,
	segment_start: Any,
	events: list[dict],
	policy: dict,
	heat_index: float | None = None,
	is_minor=False,
) -> dict:
	"""What break is due next, for the break coach.

	Returns the kind, when it is due, and how many minutes until then.
	"""
	now_dt = _parse_dt(now)
	seg_start = _parse_dt(segment_start)
	if now_dt is None or seg_start is None:
		return {"due": None}

	hours_so_far = (now_dt - seg_start).total_seconds() / 3600
	ent = entitlement(hours_so_far + 1.0, policy, is_minor)

	break_events = [ev for ev in events if ev.get("break_kind")]
	rest_taken = sum(1 for ev in break_events if ev.get("break_kind") == "Paid Rest")
	meal_taken = sum(1 for ev in break_events if ev.get("break_kind") == "Unpaid Meal")

	# v0.98.0. The minor ceiling supersedes the adult one where the policy has
	# it, and falls back where it does not — the same direction as the schedules.
	max_field = (
		"minor_max_hours_without_rest"
		if is_minor and policy.get("minor_max_hours_without_rest")
		else "max_hours_without_rest"
	)
	max_without = _as_float(policy.get(max_field)) if policy.get(max_field) else None

	# Check WA-style max hours without rest
	if max_without is not None:
		last_rest_dt = None
		for ev in break_events:
			if ev.get("break_kind") == "Paid Rest":
				dt = _parse_dt(ev.get("event_datetime"))
				if dt and (last_rest_dt is None or dt > last_rest_dt):
					last_rest_dt = dt
		since = last_rest_dt or seg_start
		hours_since_rest = (now_dt - since).total_seconds() / 3600
		remaining = max_without - hours_since_rest
		if remaining <= 0.5:
			return {
				"due": "Paid Rest",
				"urgency": "overdue" if remaining <= 0 else "imminent",
				"minutes_until": round(remaining * 60, 0),
				"reason": (
					f"WA: no more than {max_without:.0f} hours without a rest period"
					+ (" — the minor ceiling" if max_field.startswith("minor_") else "")
				),
			}

	# Check heat cool-down
	if heat_index is not None and heat_index >= 90:
		heat_rows = _schedule_rows(policy, "heat_schedule")
		for row in heat_rows:
			if _as_float(row.get("heat_index_from")) <= heat_index <= _as_float(row.get("heat_index_to")):
				every = _as_float(row.get("every_hours"), 2.0)
				last_cd_dt = None
				for ev in break_events:
					if ev.get("break_kind") in HEAT_RELIEF_KINDS:
						dt = _parse_dt(ev.get("event_datetime"))
						if dt and (last_cd_dt is None or dt > last_cd_dt):
							last_cd_dt = dt
				since = last_cd_dt or seg_start
				hours_since = (now_dt - since).total_seconds() / 3600
				if hours_since >= every * 0.75:
					return {
						"due": "Cool-Down",
						"urgency": "overdue" if hours_since >= every else "soon",
						"minutes_until": round(max(every - hours_since, 0) * 60, 0),
						"minutes_each": int(_as_float(row.get("minutes_each"))),
					}
				break

	# Check if rest or meal is due based on entitlement
	if rest_taken < ent["rest_periods"]:
		return {
			"due": "Paid Rest",
			"urgency": "upcoming",
			"rest_owed": ent["rest_periods"],
			"rest_taken": rest_taken,
			"schedule": ent["schedule"],
		}
	if meal_taken < ent["meal_periods"]:
		return {
			"due": "Unpaid Meal",
			"urgency": "upcoming",
			"meal_owed": ent["meal_periods"],
			"meal_taken": meal_taken,
			"schedule": ent["schedule"],
		}

	return {"due": None, "schedule": ent["schedule"]}


# ── 7. schedule ─────────────────────────────────────────────────────────────

#: The `break_kind` each schedule table on a Labor Break Policy owes, and the
#: order a crew takes them in when two fall at the same minute.
#:
#: THEY ARE `log_shift_break`'s OWN VOCABULARY, not a fifth set of words for the
#: same thing. A countdown ends with a foreman tapping the button, and what that
#: button posts is `log_shift_break(break_kind=...)` — so a schedule that named
#: its rows "rest" and "meal" would have made the handset carry a translation
#: table between what it was counting down to and what it then had to log. The
#: string this hands back is the string that goes straight back up.
REST_KIND = "Paid Rest"
MEAL_KIND = "Unpaid Meal"
HEAT_KIND = "Cool-Down"


def _placements(start: _dt.datetime, hours: float, periods: int) -> list:
	"""When `periods` breaks fall across `hours` of work, as datetimes.

	THE MIDPOINT OF EACH EQUAL SEGMENT, which is the placement the regulation
	describes rather than a spacing somebody chose. OAR 839-020-0050(1) says a
	rest period is to be taken "in the middle of each work period of four hours
	or major part thereof", and WAC 296-126-092 uses the same construction. Two
	rest periods over an eight-hour shift therefore fall at the two-hour and the
	six-hour mark — the middle of the first four and the middle of the second —
	which is also where an orchard has always taken them.

	THE ARITHMETIC IS THE WHOLE REASON THIS IS COMPUTED ON THE SERVER. Every
	phone on a crew doing this for itself gets the same answer only while the
	handsets agree about the shift's start time to the second, and they do not:
	`BreakSchedule` reads the start out of whatever the app last synced, and the
	v0.96.0 join-time bug existed precisely because a phone and a server
	disagreed about a second. One computation, one set of instants, seven phones
	counting down to the same minute.
	"""
	if periods <= 0 or hours <= 0:
		return []
	segment = hours / periods
	return [start + _dt.timedelta(hours=segment * (index + 0.5)) for index in range(periods)]


def _band_for(hours: float, rows: list[dict]) -> dict:
	"""The entitlement band that applies, and its minutes and paid flag."""
	return _best_band(hours, rows) or {}


def _taken_counts(events: list[dict]) -> dict:
	"""How many breaks of each kind have already been logged on this shift."""
	counts = {REST_KIND: 0, MEAL_KIND: 0, HEAT_KIND: 0}
	for event in events or []:
		kind = event.get("break_kind") or ""
		if kind in HEAT_RELIEF_KINDS:
			# THE THREE HEAT KINDS ARE ONE COUNTER, exactly as they are
			# everywhere else in this module. A crew that has just come out of
			# the shade has had its relief for that hour whichever of the three
			# words the foreman tapped — see `HEAT_RELIEF_KINDS`.
			counts[HEAT_KIND] += 1
		elif kind in counts:
			counts[kind] += 1
	return counts


def schedule(
	start: Any,
	policy: dict,
	hours: float = 8.0,
	events: list[dict] | None = None,
	now: Any = None,
	heat_index: float | None = None,
	is_minor=False,
) -> list[dict]:
	"""Every break this shift owes, and the clock time each one falls due.

	WHY THE SERVER COMPUTES THIS AND NOT THE HANDSET. A break countdown is only
	useful if a crew takes its break together, and a schedule each phone works
	out for itself drifts by whatever the phones disagree about — the shift's
	start time, the device clock, and which policy the app managed to fetch
	before it lost signal. `SERVER_CHANGES.md` §14 puts it as the requirement:
	every phone on the crew has to count down to the same second. This returns
	instants, not durations, so a phone that fetched the schedule an hour late
	still shows the same 10:05 as the one that fetched it at the tailgate.

	`hours` IS THE PLANNED LENGTH OF THE SHIFT AND NOT THE HOURS WORKED SO FAR,
	and the difference decides what is on the list at all. Entitlement bands are
	keyed on the length of the shift — six hours owes a meal, four does not — so
	computing against elapsed time would mean the meal period APPEARS on the
	schedule four hours in, which is three hours after the crew needed to know
	it was coming. A shift already closed is measured end-to-end instead; see
	the caller.

	THE STATUS IS ADVISORY AND THE INSTANT IS THE FACT. `due_at` is fixed the
	moment the shift starts and never moves; `status` and `minutes_until` are
	computed against `now` and are what the countdown bar reads. A break already
	logged is `taken`, in order — the second rest of the day matches the second
	row — because the events carry a kind and not a row number, and pairing them
	by position is what lets a crew that took its first break early still see
	the right time for its second.

	`is_minor` PICKS THE TABLE AND NOTHING ELSE ABOUT THIS FUNCTION CHANGES.
	OAR 839-021-0072 owes a worker under eighteen a 15-minute rest (an adult's is
	10) at the adult frequency — corrected in v0.302.0 per BOLI — so a minor's
	countdown is the same arithmetic over the minor rows, and `_schedule_key` falls back
	to the adult table where a policy carries no minor rows. `schedule_band` is
	on every row for the same reason `policy_source` is: the handset prints
	"Minor's schedule" beside the countdown, and a badge that claimed a band the
	server did not use would be worse than no badge.

	`policy_source` TRAVELS ON EVERY ROW rather than once at the top, because a
	heat cool-down and a rest period can come off different tables of the same
	record, and a future policy that carried only half a schedule would leave
	the app unable to say which half it was showing. The handset PRINTS this —
	a countdown that looks authoritative but is really the app's own reading of
	OAR 839-020-0050 is the kind of thing somebody quotes in a wage claim.
	"""
	begins = _parse_dt(start)
	if begins is None:
		return []
	span = max(float(hours or 0), 0.0)
	if span <= 0:
		return []

	source = str(policy.get("policy") or "").strip() or None
	taken = _taken_counts(events or [])
	now_dt = _parse_dt(now)
	rows: list[dict] = []

	for kind, key, counter in (
		(REST_KIND, "rest_schedule", REST_KIND),
		(MEAL_KIND, "meal_schedule", MEAL_KIND),
	):
		banded = _schedule_rows(policy, _schedule_key(key, is_minor)) or _schedule_rows(policy, key)
		used_minor = bool(is_minor and _schedule_rows(policy, _schedule_key(key, is_minor)))
		band = _band_for(span, banded)
		periods = int(_as_float(band.get("periods_owed")))
		minutes = int(_as_float(band.get("minutes_each")))
		for index, due in enumerate(_placements(begins, span, periods)):
			rows.append(
				{
					"break_type": kind,
					"sequence": index + 1,
					"due_at": due.strftime("%Y-%m-%d %H:%M:%S"),
					"duration_minutes": minutes,
					"is_paid": _checked(band.get("paid")),
					"policy_source": source,
					"schedule_band": "minor" if used_minor else "adult",
					"taken": index < taken[counter],
				}
			)

	# THE HEAT ROWS ARE A CADENCE, NOT A BAND, which is why they are computed
	# separately rather than folded into the loop above. A rest period is owed
	# per shift length; a cool-down is owed every `every_hours` for as long as
	# the heat index stays in its band — OAR 437-004-1131 and WAC 296-307-097
	# both write it that way. They appear only when somebody has told this
	# function what the heat index IS: a schedule that invented cool-downs for a
	# 60°F morning would train a crew to ignore the ones that matter.
	if heat_index is not None:
		band = None
		for row in _schedule_rows(policy, "heat_schedule"):
			low = _as_float(row.get("heat_index_from"))
			high = _as_float(row.get("heat_index_to"))
			if low <= heat_index <= high or (low <= heat_index and high < heat_index):
				if band is None or _as_float(row.get("minutes_each")) > _as_float(band.get("minutes_each")):
					band = row
		if band:
			every = _as_float(band.get("every_hours"), 1.0)
			if every > 0:
				minutes = int(_as_float(band.get("minutes_each")))
				periods = int(span // every)
				for index in range(periods):
					rows.append(
						{
							"break_type": HEAT_KIND,
							"sequence": index + 1,
							"due_at": (begins + _dt.timedelta(hours=every * (index + 1))).strftime(
								"%Y-%m-%d %H:%M:%S"
							),
							"duration_minutes": minutes,
							# Every heat kind is paid: it is relief from the work,
							# not time off it. `BREAK_KINDS` in `tools/shifts.py`
							# marks all three the same way.
							"is_paid": True,
							"policy_source": source,
							# A HEAT CADENCE IS NOT AGE-BANDED. -1131 and
							# WAC 296-307-097 write it about the conditions and
							# about everybody standing in them, so the cool-down
							# rows say `adult` for a minor too rather than
							# implying a minor table that does not exist.
							"schedule_band": "adult",
							"taken": index < taken[HEAT_KIND],
							"concurrent_with_rest": _checked(band.get("concurrent_with_rest")),
							"heat_index": heat_index,
						}
					)

	rows.sort(key=lambda row: (row["due_at"], row["break_type"], row["sequence"]))
	for row in rows:
		row["status"], row["minutes_until"] = _status_of(row, now_dt)
	return rows


#: How late a break may be before the coach calls it overdue rather than due.
#: Five minutes, because a crew walking out of a block takes about that long and
#: a bar that turned red the second the clock passed would be red every time.
DUE_GRACE_MINUTES = 5


def _status_of(row: dict, now: _dt.datetime | None) -> tuple:
	"""`(status, minutes_until)` for one scheduled break.

	`taken` WINS OVER EVERY CLOCK COMPARISON, which is what stops a break the
	crew has already had from going red behind them. `minutes_until` is None for
	a break already taken and for a caller who named no `now` — a signed number
	would read as a countdown to something that has happened.
	"""
	if row.get("taken"):
		return "taken", None
	due = _parse_dt(row.get("due_at"))
	if now is None or due is None:
		return "scheduled", None
	minutes = round((due - now).total_seconds() / 60)
	if minutes > 0:
		return "upcoming", minutes
	if minutes >= -DUE_GRACE_MINUTES:
		return "due", minutes
	return "overdue", minutes

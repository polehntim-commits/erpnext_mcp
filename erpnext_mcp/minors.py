# SPDX-License-Identifier: MIT
"""Who is under eighteen, what that changes, and the citation for each change.

Same contract as `breaks.py`, `bucket_bridge.py` and `payroll_gl.py`: everything
arrives as a value or a dict, nothing writes, nothing imports `frappe`. The
callers — `tools/shifts.add_worker_to_shift`, `tools/dispatch.assign_farm_task`,
`tools/employee.employee_detail`, `tools/hr.list_employees` and the
`minor_hours_approaching` alert rule — do the reading and the refusing; this
module owns the arithmetic and the regulation.

────────────────────────────────────────────────────────────────────────────
`is_minor` IS DERIVED AND IS NEVER STORED
────────────────────────────────────────────────────────────────────────────

There is no `is_minor` column on Employee and there will not be one. A stored
flag is correct on the day somebody ticks it and wrong every day afterwards: a
fifteen-year-old hired in April is sixteen in July and seventeen the following
season, and the one thing a child-labour column must not be is stale in the
direction that permits more work. `date_of_birth` is a fact that does not move;
the answer is computed from it against the day being asked about, which is why
every function here takes an `on_date`.

The other half of the same argument: a farm that does not record a date of birth
gets `None` and NOT `False`. "We do not know" and "they are an adult" are
different answers, and a module that collapsed them would clear a minor onto a
ten-hour shift because a column was empty. `describe` says which of the two it
is, and the callers say so too.

────────────────────────────────────────────────────────────────────────────
TWO BANDS, BECAUSE THE LAW HAS TWO
────────────────────────────────────────────────────────────────────────────

Fourteen and fifteen is a different set of rules from sixteen and seventeen, and
treating "minor" as one category would either forbid lawful work for the older
band or permit unlawful work for the younger. `band()` answers `"under-16"`,
`"16-17"` or `""` (an adult, or somebody too young to be employed at all — see
`MINIMUM_AGE`).

  under-16   OR: 8 h a day, 40 h a week, and 07:00 to 19:00 only.
             ORS 653.315 / OAR 839-021-0220 for the hours; 29 CFR 570.35 for
             the clock, which is the FLSA's own limit on a fourteen- or
             fifteen-year-old outside school hours.
             WA: 8 h a day, 40 h a week, 05:00 to 21:00, six days a week.
             WAC 296-131-120.

  16-17      OR: 10 h a day, 60 h a week, no time-of-day limit in agriculture.
             OAR 839-021-0104. There is no FLSA hours limit on this band in
             agriculture at all; the Oregon ceiling is the binding one, which
             is why it is the number here.
             WA: 10 h a day, FIFTY h a week, 05:00 to 22:00, six days a week.
             WAC 296-131-120 — and the fifty is the one figure in this module
             where Washington binds tighter than Oregon.

AND THE STATE IS PART OF THE ANSWER, WHICH IT WAS NOT UNTIL NOW. Every function
below takes a `work_state`; where none is given they answer from
`strictest_limits()`, the tightest figure across both states, because a table
with no state in its name must not permit what some state forbids. The refusals
built from it say so and name the one argument that fixes it — see `state_note`.

The daily figures are NON-SCHOOL-DAY figures, deliberately. A school-day limit
is three hours (ORS 653.315), and applying it would need this app to know each
worker's school calendar, which it does not and should not — so the ceiling
encoded is the one that holds during harvest, and the refusal says which figure
it used so a foreman rostering during term time can see what was assumed.

────────────────────────────────────────────────────────────────────────────
WHAT A MINOR MAY NOT BE SENT TO
────────────────────────────────────────────────────────────────────────────

`PROHIBITED_TASK_TYPES` is short, and short on purpose. Every entry names a
regulation that forbids the work by AGE rather than by training, because a list
that also carried "probably unwise" would be a list a foreman learns to override.

  Spray, either band   40 CFR §170.309(c). The 2015 Worker Protection Standard
                       sets a minimum age of EIGHTEEN for pesticide handlers
                       and for early-entry workers. It is not a training gap
                       that can be closed with a course.

  Repair, under-16     29 CFR §570.71(a). The Hazardous Occupations Orders in
                       Agriculture put power-driven machinery — tractors over
                       20 PTO horsepower, balers, augers, power take-offs —
                       out of reach of anybody under sixteen. Repair is where
                       this app's task vocabulary puts that work.

The 16-17 band is NOT barred from Repair, and that is the regulation rather than
an oversight: the HO/A orders bind under-sixteens, and a seventeen-year-old
lawfully runs a tractor on a farm in Oregon.
"""

from __future__ import annotations

import datetime as _dt

#: Under this, nothing here applies because the employment itself is the
#: problem. Twelve is the floor for hand-harvest work on a non-exempt farm
#: outside school hours with written parental consent (29 CFR §570.2(b)); below
#: it, only a parent's own farm. `band` answers `"under-16"` for anybody under
#: sixteen including these, and `describe` carries `below_minimum_age` so a
#: caller can say the stronger thing.
MINIMUM_AGE = 12

#: The age at which every rule in this module stops applying.
MAJORITY_AGE = 18

BAND_UNDER_16 = "under-16"
BAND_16_17 = "16-17"

#: The two states this app has a labour vocabulary for. The same pair
#: `tools/shifts._VALID_STATES`, `Labor Break Policy.work_state` and the
#: withholding tables use, so a farm that has told this app which state it
#: operates in has told every part of it at once.
STATES = ("OR", "WA")

#: What each band may work, and when, PER STATE. `earliest`/`latest` are
#: wall-clock strings and empty means "no limit in agriculture", which is a true
#: answer in Oregon for the older band rather than a gap.
#:
#: ────────────────────────────────────────────────────────────────────────
#: WHY THIS IS KEYED ON A STATE AND USED NOT TO BE
#: ────────────────────────────────────────────────────────────────────────
#:
#: Because the two states genuinely differ, and they differ in BOTH directions —
#: which is what makes a single table wrong rather than merely imprecise:
#:
#:   * Washington's weekly ceiling for a 16- or 17-year-old is FIFTY hours and
#:     Oregon's is sixty. A crew rostered to Oregon's figure in Franklin County
#:     is ten hours a week over a limit this app was telling them they were
#:     inside.
#:   * Washington has a CLOCK for that band — 05:00 to 22:00 — and Oregon has
#:     none at all. A single table either invented an Oregon curfew or dropped
#:     Washington's.
#:
#: The daily figures are NON-SCHOOL-DAY / NON-SCHOOL-WEEK figures in both
#: states, for the reason the module docstring gives: the school-day limits need
#: each worker's school calendar, which this app does not have and should not
#: collect. Every refusal says which figure it used.
LIMITS_BY_STATE = {
	"OR": {
		BAND_UNDER_16: {
			"daily_hours": 8.0,
			"weekly_hours": 40.0,
			"earliest": "07:00",
			"latest": "19:00",
			"max_days_per_week": None,
			"citation": "ORS 653.315 / OAR 839-021-0220; 29 CFR 570.35",
		},
		BAND_16_17: {
			"daily_hours": 10.0,
			"weekly_hours": 60.0,
			"earliest": "",
			"latest": "",
			"max_days_per_week": None,
			"citation": "OAR 839-021-0104",
		},
	},
	#: WAC 296-131-120, "Hours of work for minors in agriculture", read against
	#: the rule text rather than a summary of it. The four numbers that matter:
	#: under-16 may work up to eight hours a day and forty a week during weeks
	#: when school is not in session and "may not be employed before 5:00 a.m.
	#: nor after 9:00 p.m."; 16- and 17-year-olds up to ten a day and FIFTY a
	#: week, and "may not be employed before 5:00 a.m. nor after 10:00 p.m."
	#:
	#: THE FIFTY IS THE ONE TO NOTICE. It is the only figure here that is
	#: STRICTER than Oregon's, and it is the reason a state-blind table was not
	#: merely untidy — an app carrying Oregon's sixty into Washington reports a
	#: lawful roster for a week that is ten hours over.
	"WA": {
		BAND_UNDER_16: {
			"daily_hours": 8.0,
			"weekly_hours": 40.0,
			"earliest": "05:00",
			"latest": "21:00",
			"max_days_per_week": 6,
			"citation": "WAC 296-131-120",
		},
		BAND_16_17: {
			"daily_hours": 10.0,
			"weekly_hours": 50.0,
			"earliest": "05:00",
			"latest": "22:00",
			"max_days_per_week": 6,
			"citation": "WAC 296-131-120",
		},
	},
}

#: `max_days_per_week` IS SET FOR WASHINGTON AND None FOR OREGON, and the
#: asymmetry is deliberate rather than half-finished. WAC 296-131-120(4) states
#: the six-day limit plainly, in the same rule as the hours above. Oregon's
#: six-day sentence lives in OAR 839-021-0290 — which is ALSO the rule that puts
#: an under-16's non-school-day ceiling at ten hours and sixty a week, not the
#: eight and forty this app has shipped since v0.98.0 under a different
#: citation. Encoding one sentence out of a rule whose other numbers contradict
#: the table above would be picking the half that suits; the discrepancy is
#: written down here so somebody can settle it against counsel rather than
#: against a search result. Until then Oregon carries no days-per-week figure
#: and this app says nothing about it, which is the honest of the two silences.
#:
#: IT IS A WARNING AND NEVER A REFUSAL, wherever it is read. WAC 296-131-120(4)
#: carves out dairy, livestock, hay harvest and irrigation-dependent crop work,
#: and this app does not know which of those a given shift is — so a hard block
#: would be a false refusal on exactly the operations the exception was written
#: for.


def strictest_limits() -> dict:
	"""The tightest figure from every state, per band. What an UNKNOWN state gets.

	NOT A JURISDICTION. No farm is subject to this table; it is what this app
	applies when nothing has said which state the work is in, and it is built by
	taking the smallest ceiling and the narrowest clock across `LIMITS_BY_STATE`
	rather than by hand, so a state added later cannot leave it stale.

	WHY THE STRICT DIRECTION AND NOT A DEFAULT STATE. The same argument the
	module docstring makes about a missing date of birth: "we do not know" and
	"they may work" are different answers, and defaulting an unrecorded state to
	Oregon would clear a Washington seventeen-year-old onto a 60-hour week that
	state does not allow. The cost is the mirror image — an Oregon crew with no
	`work_state` on the shift is held to Washington's fifty — so every refusal
	built from this table SAYS it was built from this table and names the one
	argument that fixes it. See `state_note`.
	"""
	bands = {}
	for band_name in (BAND_UNDER_16, BAND_16_17):
		tables = [state[band_name] for state in LIMITS_BY_STATE.values()]
		earliest = [table["earliest"] for table in tables if table["earliest"]]
		latest = [table["latest"] for table in tables if table["latest"]]
		days = [table["max_days_per_week"] for table in tables if table["max_days_per_week"]]
		bands[band_name] = {
			"daily_hours": min(table["daily_hours"] for table in tables),
			"weekly_hours": min(table["weekly_hours"] for table in tables),
			# The LATEST start and the EARLIEST finish — the narrowest window any
			# state allows, which is the intersection and not the union.
			"earliest": max(earliest) if earliest else "",
			"latest": min(latest) if latest else "",
			"max_days_per_week": min(days) if days else None,
			# EVERY STATE'S CITATION IN FULL, not the first clause of each. The
			# strictest table takes its daily figure from one rule and its clock
			# from another, so no single citation is the whole answer — and
			# trimming at the semicolon dropped `29 CFR 570.35`, which is the
			# authority for the 07:00 start this row actually enforces.
			"citation": " / ".join(sorted({table["citation"] for table in tables})),
		}
	return bands


#: What a caller that has not said which state gets. See `strictest_limits`.
#:
#: THIS CONSTANT CHANGED MEANING IN THIS RELEASE. It was Oregon's table under a
#: state-blind name; it is now the strictest-across-states table, because the
#: name has no state in it and the thing a nameless table must not do is permit
#: what some state forbids. Oregon's own figures are `LIMITS_BY_STATE["OR"]` and
#: are unchanged to the digit.
LIMITS = strictest_limits()


def limits_for_band(work_band: str, work_state: str = "") -> dict:
	"""One band's limits in one state, or the strictest where the state is unknown.

	An unrecognised state answers the strictest table rather than raising: this
	is read on the refusal path, and a compliance check that threw because a
	column held something unexpected would fail open at the worst moment.
	"""
	table = LIMITS_BY_STATE.get(str(work_state or "").strip().upper()) or LIMITS
	return dict(table.get(work_band) or {})


def state_note(work_state: str) -> str:
	"""The sentence a refusal owes somebody when no state was recorded. "" if one was.

	A refusal a foreman cannot act on is a refusal they route around, and
	"stricter of Oregon and Washington" is unactionable on its own — so this
	names the column and the two values it takes.
	"""
	if str(work_state or "").strip().upper() in LIMITS_BY_STATE:
		return ""
	return (
		"NO work_state IS RECORDED for this shift, so the STRICTER of Oregon and Washington was "
		"applied — Washington's 50-hour week for the 16-17 band is the binding figure, and "
		"Oregon allows 60. If this crew is in Oregon, set work_state on the shift (OR or WA) and "
		"the state's own ceiling is used instead."
	)


#: How close to a ceiling is close enough to say so. An hour of the day and four
#: of the week — one more pick round and one more afternoon respectively, which
#: is the granularity a foreman can actually act on. Warnings, never refusals.
DAILY_WARNING_HOURS = 1.0
WEEKLY_WARNING_HOURS = 4.0

#: Farm Task `task_type` → the bands it is closed to, with the citation.
PROHIBITED_TASK_TYPES = {
	"Spray": (
		(BAND_UNDER_16, BAND_16_17),
		"40 CFR §170.309(c) — the Worker Protection Standard sets a minimum age of 18 for "
		"pesticide handlers and for early-entry workers. It is an age bar, not a training gap: "
		"there is no course that closes it.",
	),
	"Repair": (
		(BAND_UNDER_16,),
		"29 CFR §570.71(a) — the Hazardous Occupations Orders in Agriculture put power-driven "
		"machinery out of reach of anybody under sixteen. A 16- or 17-year-old may lawfully do "
		"this work in Oregon.",
	),
}


#: The rows OAR 839-021-0072 asks for, as a `Labor Break Schedule Row` table
#: would hold them. PUBLISHED, NOT SEEDED — and the difference is the point.
#:
#: `Labor Break Policy` carries `human_approved_by` because a break schedule is a
#: statement this operation makes about what it owes its crew, and an app that
#: wrote rows into an approved policy on `bench migrate` would move that
#: statement without anybody signing it. So nothing here is ever written by this
#: app; `get_break_policy` returns these BESIDE a policy that has no minor rows,
#: marked as unapproved, so the gap is visible on the handset and the operator
#: has the rows to paste rather than a citation to go and read.
#:
#: v0.302.0 (Tim, 2026-10-10: align with BOLI). CORRECTED: OAR 839-021-0072 owes a minor a rest period of not less
#: than FIFTEEN minutes and a 30-minute meal "in the manner prescribed by OAR 839-020-0050" — the ADULT frequency,
#: not a rest every two hours (BOLI: "Rest breaks for all minors are 15 minutes rather than 10"). So the minor tables
#: are the adult BOLI chart with 15-minute rests.
#:
#: BOLI's chart (oregon.gov/boli/workers/Pages/meals-and-breaks.aspx; OAR 839-020-0050): a paid rest for each
#: segment of four hours or major part thereof, a 30-minute unpaid meal from six hours (a second at 14, a third at 22).
#: Bands start just past a boundary (2.01 h) because "2 hrs or less" owes none and exactly 6, 14 and 22 hours keep the
#: lower rest count.
OR_REST_SCHEDULE = (
	{"hours_from": 2.01, "hours_to": 6.0, "periods_owed": 1, "minutes_each": 10, "paid": 1},
	{"hours_from": 6.01, "hours_to": 10.0, "periods_owed": 2, "minutes_each": 10, "paid": 1},
	{"hours_from": 10.01, "hours_to": 14.0, "periods_owed": 3, "minutes_each": 10, "paid": 1},
	{"hours_from": 14.01, "hours_to": 18.0, "periods_owed": 4, "minutes_each": 10, "paid": 1},
	{"hours_from": 18.01, "hours_to": 22.0, "periods_owed": 5, "minutes_each": 10, "paid": 1},
	{"hours_from": 22.01, "hours_to": 24.0, "periods_owed": 6, "minutes_each": 10, "paid": 1},
)
OR_MEAL_SCHEDULE = (
	{"hours_from": 6.0, "hours_to": 13.99, "periods_owed": 1, "minutes_each": 30, "paid": 0},
	{"hours_from": 14.0, "hours_to": 21.99, "periods_owed": 2, "minutes_each": 30, "paid": 0},
	{"hours_from": 22.0, "hours_to": 24.0, "periods_owed": 3, "minutes_each": 30, "paid": 0},
)
OR_SCHEDULE_CITATION = "OAR 839-020-0050; BOLI meals and breaks chart"

MINOR_REST_SCHEDULE = tuple({**row, "minutes_each": 15} for row in OR_REST_SCHEDULE)
MINOR_MEAL_SCHEDULE = tuple(dict(row) for row in OR_MEAL_SCHEDULE)

#: The citation the two tables above come from, quoted wherever they are.
MINOR_SCHEDULE_CITATION = "OAR 839-021-0072"


# ── parsing ───────────────────────────────────────────────────────────────────


def _as_date(value):
	"""A date from whatever the column or the argument held, or None.

	Accepts a `date`, a `datetime`, `YYYY-MM-DD`, and a MariaDB DATETIME string —
	the last because a caller may hand this a shift's `start_datetime` as the day
	being asked about, and re-parsing it in three call sites would be three
	chances to disagree.
	"""
	if isinstance(value, _dt.datetime):
		return value.date()
	if isinstance(value, _dt.date):
		return value
	text = str(value or "").strip()
	if not text:
		return None
	head = text.replace("T", " ").split(" ")[0]
	try:
		return _dt.datetime.strptime(head, "%Y-%m-%d").date()
	except ValueError:
		return None


def _as_datetime(value):
	if isinstance(value, _dt.datetime):
		return value
	text = str(value or "").strip()
	if not text:
		return None
	# `T` normalised to a space FIRST, so one format list covers both spellings.
	# The iOS app posts ISO-8601 and MariaDB stores a space; a parser that knew
	# only one of them would silently fall through to the date-only branch below
	# and read every afternoon shift as starting at midnight.
	text = text.replace("T", " ")
	for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
		try:
			return _dt.datetime.strptime(text, fmt)
		except ValueError:
			continue
	day = _as_date(value)
	return _dt.datetime(day.year, day.month, day.day) if day else None


def _minutes(clock: str):
	""" "07:00" → 420. Empty or unparseable → None, which reads as "no limit"."""
	parts = str(clock or "").strip().split(":")
	if len(parts) < 2:
		return None
	try:
		return int(parts[0]) * 60 + int(parts[1])
	except ValueError:
		return None


# ── age ───────────────────────────────────────────────────────────────────────


def age_on(date_of_birth, on_date) -> int | None:
	"""Completed years on `on_date`, or None where either date is unreadable.

	Birthday arithmetic done by comparing (month, day) tuples rather than by
	dividing days by 365.25: the second is wrong for one day in four on somebody
	born in a leap year, and the day it is wrong is their birthday.
	"""
	born = _as_date(date_of_birth)
	when = _as_date(on_date)
	if born is None or when is None:
		return None
	years = when.year - born.year
	if (when.month, when.day) < (born.month, born.day):
		years -= 1
	return years


def band(date_of_birth, on_date) -> str:
	"""`"under-16"`, `"16-17"`, or `""` for an adult or an unknown birth date.

	AN UNKNOWN BIRTH DATE ANSWERS `""` HERE and `None` from `is_minor`. This
	function's answer feeds `LIMITS`, and a limit lookup has to be total; the
	"we do not know" distinction is `is_minor`'s and `describe`'s to carry, and
	every caller that refuses reads one of those rather than this.
	"""
	age = age_on(date_of_birth, on_date)
	if age is None or age >= MAJORITY_AGE:
		return ""
	return BAND_UNDER_16 if age < 16 else BAND_16_17


def is_minor(date_of_birth, on_date) -> bool | None:
	"""True, False, or None where the birth date is missing or unreadable.

	THREE-VALUED ON PURPOSE. See the module docstring: a farm with no date of
	birth on file has not told this app that somebody is an adult.
	"""
	age = age_on(date_of_birth, on_date)
	if age is None:
		return None
	return age < MAJORITY_AGE


def limits_for(date_of_birth, on_date, work_state: str = "") -> dict:
	"""The band's limits, or an empty dict for an adult or an unknown birth date."""
	return limits_for_band(band(date_of_birth, on_date), work_state)


def describe(date_of_birth, on_date, work_state: str = "") -> dict:
	"""Everything a roster row, an employee read or a refusal needs, in one shape.

	`is_minor` is the key iOS branches on for the purple badge. It is `None` where
	nothing is recorded, and `date_of_birth_recorded` says which case that is —
	a client rendering `is_minor == true` as a badge renders nothing for either,
	which is right, and a foreman reading the roster can still see the gap.
	"""
	age = age_on(date_of_birth, on_date)
	which = band(date_of_birth, on_date)
	state = str(work_state or "").strip().upper()
	out = {
		"date_of_birth_recorded": bool(_as_date(date_of_birth)),
		"age": age,
		"is_minor": None if age is None else age < MAJORITY_AGE,
		"minor_band": which or None,
		"minor_limits": limits_for_band(which, state) or None,
		# WHICH TABLE THE FIGURES BESIDE THIS CAME FROM. A roster row carrying a
		# ceiling and not the jurisdiction it is a ceiling in cannot be checked by
		# the person reading it, and the two states disagree about the 16-17
		# weekly figure by ten hours. None means no state was recorded and the
		# strictest table was used.
		"minor_limits_state": state if state in LIMITS_BY_STATE else None,
	}
	if age is not None and age < MINIMUM_AGE:
		out["below_minimum_age"] = True
	return out


# ── the clock ─────────────────────────────────────────────────────────────────


def time_of_day_violation(work_band: str, start, end="", work_state: str = "") -> str | None:
	"""Why this span is outside the hours the band may work, or None.

	Checked against the START and the END separately rather than against the
	span, because those are the two the regulation names and because a span
	crossing midnight is a different violation with a different sentence — one
	this returns on the end check, since 00:30 is before 07:00.
	"""
	limits = limits_for_band(work_band, work_state)
	earliest = _minutes(limits.get("earliest"))
	latest = _minutes(limits.get("latest"))
	if earliest is None and latest is None:
		return None

	for value, label in ((start, "starts"), (end, "ends")):
		moment = _as_datetime(value)
		if moment is None:
			continue
		minute = moment.hour * 60 + moment.minute
		if earliest is not None and minute < earliest:
			return (
				f"the shift {label} at {moment.strftime('%H:%M')}, before {limits['earliest']}. "
				f"A worker in the {work_band} band may not work earlier ({limits['citation']})."
			)
		if latest is not None and minute > latest:
			return (
				f"the shift {label} at {moment.strftime('%H:%M')}, after {limits['latest']}. "
				f"A worker in the {work_band} band may not work later ({limits['citation']})."
			)
	return None


def hours_violation(
	work_band: str, hours_today: float, hours_this_week: float, work_state: str = ""
) -> str | None:
	"""Why these totals are over the band's ceiling, or None.

	The DAILY figure is reported first where both are over, because it is the one
	a foreman can still do something about before the crew starts.
	"""
	limits = limits_for_band(work_band, work_state)
	if not limits:
		return None
	daily = float(limits["daily_hours"])
	weekly = float(limits["weekly_hours"])
	if hours_today > daily:
		return (
			f"that would be {hours_today:.1f} hours today against a {daily:.0f}-hour non-school-day "
			f"ceiling for the {work_band} band ({limits['citation']})."
		)
	if hours_this_week > weekly:
		return (
			f"that would be {hours_this_week:.1f} hours this week against a {weekly:.0f}-hour "
			f"ceiling for the {work_band} band ({limits['citation']})."
		)
	return None


def hours_warning(
	work_band: str, hours_today: float, hours_this_week: float, work_state: str = ""
) -> str | None:
	"""How close to the ceiling this is, where it is close but not over.

	Returns None once it is actually over — that is `hours_violation`'s sentence,
	and two messages about one fact is how a warning stops being read.
	"""
	limits = limits_for_band(work_band, work_state)
	if not limits or hours_violation(work_band, hours_today, hours_this_week, work_state):
		return None
	daily = float(limits["daily_hours"])
	weekly = float(limits["weekly_hours"])
	if daily - hours_today <= DAILY_WARNING_HOURS:
		return (
			f"{hours_today:.1f} of {daily:.0f} hours for the day. "
			f"{daily - hours_today:.1f} hour(s) left before the {work_band} ceiling."
		)
	if weekly - hours_this_week <= WEEKLY_WARNING_HOURS:
		return (
			f"{hours_this_week:.1f} of {weekly:.0f} hours for the week. "
			f"{weekly - hours_this_week:.1f} hour(s) left before the {work_band} ceiling."
		)
	return None


def days_warning(work_band: str, days_this_week: int, work_state: str = "") -> str | None:
	"""Whether this many days in the week is over the band's limit. A WARNING.

	NEVER A REFUSAL, and the reason is in the block beside `LIMITS_BY_STATE`:
	WAC 296-131-120(4) carves out dairy, livestock, hay harvest and
	irrigation-dependent crop work from the six-day rule, and this app cannot
	tell which of those a shift is. Refusing a seventh day on a dairy would be a
	false refusal on precisely the operation the exception was written for — so
	this says the number and the citation and lets the foreman answer it.

	Oregon carries no figure, so this is silent there. See `LIMITS_BY_STATE`.
	"""
	limits = limits_for_band(work_band, work_state)
	ceiling = limits.get("max_days_per_week")
	if not ceiling or days_this_week <= int(ceiling):
		return None
	return (
		f"that would be {days_this_week} days this week against a {int(ceiling)}-day limit for the "
		f"{work_band} band ({limits['citation']}). Dairy, livestock, hay harvest and "
		"irrigation-dependent crop work are excepted from it, so this is a note and not a refusal "
		"— but if this crew is none of those, the seventh day is one too many."
	)


# ── the work ──────────────────────────────────────────────────────────────────


def prohibited_reason(work_band: str, task_type: str) -> str | None:
	"""Why this band may not be sent to this kind of task, or None.

	An unknown task type answers None. This app's Farm Task Select has eleven
	values and two of them are named here; inventing a refusal for the other nine
	— or for a value an operator added — would be this module deciding policy it
	has no citation for.
	"""
	if not work_band:
		return None
	entry = PROHIBITED_TASK_TYPES.get(str(task_type or "").strip())
	if not entry:
		return None
	bands, citation = entry
	return citation if work_band in bands else None

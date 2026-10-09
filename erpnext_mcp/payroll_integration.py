# SPDX-License-Identifier: MIT
"""Timesheet → payroll. PURE FUNCTIONS.

No database reads, no side effects, fully testable with deterministic inputs.
Same contract as `payroll_calc.py`, `form_generators.py` and `withholding.py`:
everything arrives as an argument.

v0.35.0. THE GAP THIS CLOSES. v0.30.0 built a payroll engine that could compute
a slip from hours, and v0.19.3 built a shift register that recorded them — and
nothing joined the two. `tools/payroll.py::_load_shifts` reached for Farm Shift
and came back with the crew-wide span for every worker, zero overtime and zero
piece units, so every figure past "how long was the shift" had to be keyed in by
hand. A payroll run that asks somebody to retype what the foreman already
recorded is a payroll run with two sources of truth and one of them wrong.

────────────────────────────────────────────────────────────────────────────
THE UNIT OF WORK IS A SEGMENT, NOT A SHIFT
────────────────────────────────────────────────────────────────────────────

A shift is crew-shaped and payroll is person-shaped, and `shifts.py` already
resolved that: every crew row carries its own `joined_at` and `left_at`. So the
thing this module aggregates is a SEGMENT — one person's own span on one shift —
and "the crew worked 06:00 to 15:00" and "Ana joined at 07:10 and left at 13:00"
stay two different facts, as they are on the record. Paying Ana for the crew's
nine hours would be paying her for two she was not there for, and paying the
crew for Ana's six would be the wage claim.

A crew row with no `joined_at` inherits the shift's start; one with no `left_at`
inherits the shift's end. Both are the ordinary case — most people work the whole
shift — and neither is an assumption, because that is what the close writes.

────────────────────────────────────────────────────────────────────────────
BREAKS COME IN TWO KINDS AND THEY DO NOT BEHAVE THE SAME
────────────────────────────────────────────────────────────────────────────

`break_hours` is PAID rest. It stays inside `total_hours`, because the worker is
on the clock, and it is handed to `payroll_calc` separately so the piece-rate
path can pay it at the average hourly rate the period earned — WAC 296-131-020,
and Oregon's OAR 839-020-0050 to the same effect. A piece-rate worker paid
nothing for a ten-minute break is a piece-rate worker paid nothing for a
ten-minute break, whatever the buckets say.

`unpaid_break_hours` is the meal period. It is SUBTRACTED from the span, because
the worker is not on the clock and those minutes are neither hours worked nor
hours toward the overtime threshold.

Conflating the two is the standard way a farm ends up owing back wages, so they
are two fields and never one.

────────────────────────────────────────────────────────────────────────────
OVERTIME IS A WEEKLY QUESTION ANSWERED CHRONOLOGICALLY
────────────────────────────────────────────────────────────────────────────

Oregon HB 4002 and Washington SB 5172 both put agricultural overtime at 40 hours
in a WORKWEEK, both fully phased in as of 2025, and both at 1.5x. A pay period is
not a workweek: a biweekly period is two of them, and forty-five hours in week
one and thirty-five in week two is five hours of overtime, not zero. Summing the
period and comparing to eighty would find none of it.

So the segments are bucketed into seven-day workweeks anchored at
`pay_period_start` — overridable with `workweek_anchor` for an employer whose
declared workweek does not line up with the pay period — and each week is walked
IN TIME ORDER, with the hours past the fortieth marked overtime.

Chronological rather than proportional, and that matters for exactly one reason:
it also decides WHICH STATE the overtime was worked in. A picker who spends
Monday to Thursday in Oregon and Friday in Washington crossed forty on Friday, so
the premium is Washington's hours and Washington's tax allocation. Splitting the
overtime across both states pro rata would be tidier arithmetic about a day that
did not happen.

────────────────────────────────────────────────────────────────────────────
WHAT THIS DOES NOT DECIDE
────────────────────────────────────────────────────────────────────────────

THE OVERTIME PREMIUM FOR PIECE WORK is `payroll_calc`'s. As of v0.49.0 it is the
FLSA half-time premium of 29 CFR 778.111 — the piece earnings already paid
straight time for the overtime hours, so what is owed on top is half the regular
rate. Releases through v0.48.2 paid the full 1.5x on top, which is more than the
law asks and could not be reconciled with the weighted-average method a mixed
piece-and-hourly day needs. Both are half-time now, and they agree.

MAKEUP PAY FOR A MINIMUM-WAGE SHORTFALL IS PAID as of v0.49.0, and this module no
longer decides it. `payroll_calc.calculate_full_payroll` applies the higher-of
rule — gross is the greater of what the work earned and what the hours are owed,
per state, with the overtime premium in the floor — and carries the difference as
`minimum_wage_makeup`. `check_minimum_wage_by_state` is now the independent check
on the result rather than the only place the floor appeared: it tests the wages as
paid and should find nothing.

The reason the old posture existed still holds and is still served. A top-up that
vanished into gross would hide a piece rate set below the lawful floor, so it does
not vanish: it is its own figure on the slip, its own column on the stored row,
and its own list — `topped_up_to_minimum_wage` — at the top of the run summary.
What changed is that the farm no longer owes the money while the fact is visible.

WHO IS OWED ANYTHING AT ALL. An employee with shifts and no salary structure is
not paid zero — they are REPORTED, by name, in `employees_missing_structures`.
Zero is a number, and a number nobody questions.

────────────────────────────────────────────────────────────────────────────
A DAY CAN BE PAID TWO WAYS AND USUALLY IS AT SOME POINT IN A SEASON
────────────────────────────────────────────────────────────────────────────

`pay_type` was one field on one salary structure until v0.49.0, so a picker who
spent the morning on buckets and the afternoon on irrigation was paid the piece
rate for the afternoon — which is to say nothing, because irrigation produces no
buckets. A segment now carries its own `pay_type` and its own `pay_rate` where the
record says so, blank where it does not, and blank is the ordinary day.

Segments are grouped for the engine by (state, pay type, rate) rather than by
state alone, and the engine pays each group its own way and then takes ONE regular
rate across the lot for the overtime premium — 29 CFR 778.115, the method for an
employee working at two rates in one workweek. Six hours of picking at $1.50 a
bucket and two hours of irrigation at $16.00 is $167, and the minimum wage floor
is tested on all eight hours, not on the six that made buckets.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from . import bucket_bridge
from .payroll_calc import (
	applicable_minimum_wage,
	calculate_full_payroll,
	minimum_wage_floor,
)

#: Hours in a workweek before the ag overtime premium starts. Oregon HB 4002 and
#: Washington SB 5172 both landed here, both fully phased as of 2025.
OVERTIME_THRESHOLD_HOURS = 40.0

#: Days in a workweek. FLSA's is any fixed, recurring 168-hour period; both state
#: schemes inherit that, so the length is fixed and only the anchor varies.
WORKWEEK_DAYS = 7

#: The W-4 an employee with none on file is treated as having: single, no
#: adjustments. Same shape and same defaults as `tools/payroll.py::_load_w4_data`
#: falls back to, restated here because this module reads no database.
DEFAULT_W4 = {
	"filing_status": "Single",
	"multiple_jobs": False,
	"additional_income_from_other_jobs": 0,
	"dependents_under_17_count": 0,
	"other_dependents_count": 0,
	"total_dependents_credit": 0,
	"other_income": 0,
	"deductions": 0,
	"extra_withholding_per_period": 0,
}

#: Keys a piece-unit row might carry its count under, in the order they are
#: tried. A Bucket Log Entry row that carries none of them is ONE bucket, which
#: is what the record means: the row is the bucket.
_UNIT_KEYS = ("piece_units", "units", "quantity", "qty", "bucket_count", "count", "bins")

#: Keys a piece-unit row might name its worker under. `picker_id` is the
#: BucketLog bridge's; `assigned_to` is Farm Task Assignment's.
_WORKER_KEYS = ("employee", "picker_id", "picker", "assigned_to", "worker")

#: Keys a shift or crew row might carry a rate override under. Present only where
#: the day was paid at something other than the salary structure's own rate —
#: irrigation hours at $16.00 under a structure that pays $1.50 a bucket.
_RATE_KEYS = ("pay_rate", "hourly_rate", "rate", "base_rate")


# ── Parsing ───────────────────────────────────────────────────────────────


def _as_datetime(value) -> datetime | None:
	"""A datetime from whatever the caller had — or None, never a raise.

	Frappe hands back `datetime` objects, a fixture hands back strings, and a
	form that was never filled in hands back None. A timesheet aggregator that
	raised on the third would take a whole payroll run down over one shift
	somebody forgot to close.
	"""
	if value is None or value == "":
		return None
	if isinstance(value, datetime):
		return value
	if isinstance(value, date):
		return datetime(value.year, value.month, value.day)
	text = str(value).strip()
	if not text:
		return None
	text = text.replace("T", " ")
	if "." in text:
		text = text.split(".")[0]
	for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
		try:
			return datetime.strptime(text, fmt)
		except ValueError:
			continue
	return None


def _as_date(value) -> date | None:
	"""A date from whatever the caller had, or None."""
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	parsed = _as_datetime(value)
	return parsed.date() if parsed else None


def _as_float(value, default: float = 0.0) -> float:
	try:
		if value is None or value == "":
			return default
		return float(value)
	except (TypeError, ValueError):
		return default


def _first_present(row: dict, keys) -> float | None:
	"""The first of `keys` the row actually carries, as a float.

	`row.get(k) or 0` would be wrong here: a bucket log that genuinely recorded
	zero units is a different fact from one that has no units column at all, and
	only the second should fall through to the next key.
	"""
	for key in keys:
		if key in row and row[key] not in (None, ""):
			return _as_float(row[key])
	return None


def _row_worker(row: dict) -> str:
	for key in _WORKER_KEYS:
		value = row.get(key)
		if value:
			return str(value)
	return ""


def _row_units(row: dict) -> float:
	"""Units on one piece row. A row with no count column is one unit.

	The BucketLog bridge is written by whichever version of the iPad app is in
	the field this season, so the column may or may not be there. Where it is
	not, the row itself is the unit — one entry, one bucket — which is what the
	record has always meant and is the only reading that does not silently pay
	somebody nothing for a day of picking.
	"""
	units = _first_present(row, _UNIT_KEYS)
	return 1.0 if units is None else units


# ── BucketLog attachment ─────────────────────────────────────────────────


def attach_bucket_log_entries(shifts: list[dict], entries: list[dict]) -> tuple[list[dict], list[dict]]:
	"""Bucket Log Entry rows, matched onto the shift each was worked on and
	attached as `bucket_logs` — the key `_piece_units_for` already reads.

	PURE: both `shifts` and `entries` arrive already fetched; the caller
	(`tools/bucket_log.py`, or a caller assembling a shift dict by hand) is what
	queries Bucket Log Entry out of the database. `bucket_bridge.py` owns the
	Accepted-only filter and the reshape into a `bucket_logs` row; this
	function's own job is the join a Bucket Log Entry cannot do for itself — it
	carries an employee and a timestamp, not a shift, until
	`link_entries_to_shift` sets one, and even then this still matches by
	(employee, date) rather than trusting that field, for the same reason
	`tools/payroll.py::_attach_piece_rows` does for its other piece sources:
	neither carries a link this app did not add itself.

	Entries matching no shift in the list are NOT dropped. The second return
	value is what was left over, for the caller to decide what to do with —
	`tools/payroll.py::_orphan_piece_shifts` already knows how to turn an
	unmatched piece row into a zero-hour stand-in shift, and this module does
	not repeat that pattern for a second source.

	Returns `(shifts, unmatched)`. `shifts` is a NEW list — the input shift
	dicts are not mutated — each one carrying a `bucket_logs` list where at
	least one entry matched it.
	"""
	shifts = [dict(shift) for shift in shifts or []]

	index: dict[tuple, dict] = {}
	for shift in shifts:
		day = str(shift.get("start_datetime") or shift.get("start") or "")[:10]
		for row in _crew_of(shift):
			employee = str(row.get("employee") or "")
			if employee:
				index.setdefault((employee, day), shift)

	by_key: dict[tuple, list[dict]] = {}
	for entry in entries or []:
		employee = str(entry.get("employee") or "")
		day = str(entry.get("timestamp") or "")[:10]
		if employee and day:
			by_key.setdefault((employee, day), []).append(entry)

	unmatched: list[dict] = []
	for key, rows in by_key.items():
		shift = index.get(key)
		if shift is None:
			unmatched.extend(rows)
			continue
		shift.setdefault("bucket_logs", []).extend(bucket_bridge.entries_to_payroll_shape(rows))

	return shifts, unmatched


# ── Segments: one person's own span on one shift ──────────────────────────


class _Segment:
	"""One employee's own stretch of one shift. The unit everything counts."""

	__slots__ = (
		"break_hours",
		"employee",
		"employee_name",
		"end",
		"hours",
		"open_ended",
		"pay_rate",
		"pay_type",
		"piece_units",
		"shift",
		"start",
		"unpaid_break_hours",
		"work_state",
	)

	def __init__(self, **kwargs):
		for slot in self.__slots__:
			setattr(self, slot, kwargs.get(slot))

	def as_dict(self) -> dict:
		return {
			"shift": self.shift,
			"employee": self.employee,
			"employee_name": self.employee_name,
			"work_state": self.work_state,
			"start": str(self.start) if self.start else None,
			"end": str(self.end) if self.end else None,
			"hours": round(self.hours, 2),
			"break_hours": round(self.break_hours, 2),
			"unpaid_break_hours": round(self.unpaid_break_hours, 2),
			"piece_units": round(self.piece_units, 2),
			"pay_type": self.pay_type or "",
			"pay_rate": self.pay_rate,
			"open_ended": self.open_ended,
		}


def _crew_of(shift: dict) -> list[dict]:
	"""The crew rows of a shift, or the shift itself where it names one worker.

	The second form is what `tools/payroll.py` passed before this release and
	what a test writes by hand: a flat dict with `employee` and `hours` on it. It
	stays supported because a one-worker shift is a real thing and making every
	caller wrap it in a crew of one would be ceremony.
	"""
	crew = shift.get("crew")
	if crew:
		return [dict(row) for row in crew]
	if shift.get("employee"):
		return [
			{
				"employee": shift.get("employee"),
				"employee_name": shift.get("employee_name"),
				"joined_at": shift.get("joined_at"),
				"left_at": shift.get("left_at"),
			}
		]
	return []


def _piece_units_for(shift: dict, employee: str) -> float:
	"""Every piece unit on this shift attributable to this worker.

	Three sources, summed rather than preferred: a count on the crew row itself,
	a `piece_units_by_employee` map on the shift, and rows in any of the piece
	lists — Farm Tasks, bucket logs, whatever the loader attached. A farm that
	records buckets two ways during a season transition should be paid for both,
	not for whichever this function happened to check first.
	"""
	total = 0.0
	mapping = shift.get("piece_units_by_employee") or {}
	if employee in mapping:
		total += _as_float(mapping[employee])
	for key in ("piece_rows", "tasks", "bucket_logs", "piece_logs"):
		for row in shift.get(key) or []:
			if _row_worker(row) == employee:
				total += _row_units(row)
	return total


def _segments_of(shift: dict) -> list[_Segment]:
	"""Explode one shift into one segment per crew member."""
	shift_start = _as_datetime(shift.get("start_datetime") or shift.get("start"))
	shift_end = _as_datetime(shift.get("end_datetime") or shift.get("end"))
	work_state = str(shift.get("work_state") or "").strip().upper()
	shift_name = shift.get("name") or shift.get("shift") or ""

	segments = []
	for row in _crew_of(shift):
		employee = str(row.get("employee") or "")
		if not employee:
			continue

		start = _as_datetime(row.get("joined_at")) or shift_start
		end = _as_datetime(row.get("left_at")) or shift_end
		open_ended = end is None

		paid_break = _as_float(
			row.get("break_hours", shift.get("break_hours")),
			0.0,
		)
		unpaid_break = _as_float(
			row.get("unpaid_break_hours", shift.get("unpaid_break_hours")),
			0.0,
		)

		# An explicit `hours` short-circuits the span arithmetic. That is the
		# v0.30.0 shape and it is also how a correction gets in: a foreman who
		# fixes somebody's hours after the fact has a number, not a timestamp.
		explicit = row.get("hours", shift.get("hours"))
		if explicit not in (None, ""):
			hours = max(_as_float(explicit), 0.0)
		elif start and end and end > start:
			hours = max((end - start).total_seconds() / 3600.0 - unpaid_break, 0.0)
		else:
			hours = 0.0

		# A paid break cannot exceed the span it sat inside. Where the record
		# says it did, the span wins — the clock is the harder fact — and the
		# piece-rate break payment is capped at what was actually worked rather
		# than paying a rest period longer than the shift.
		paid_break = min(max(paid_break, 0.0), hours)

		# What this stretch was paid BY, where the record says. Blank is the
		# ordinary case and means "the way this worker's salary structure says" —
		# the field only has to be filled in on the day that was different, which
		# is the day a picker spent on irrigation.
		pay_type = str(row.get("pay_type") or shift.get("pay_type") or "").strip()
		pay_rate = _first_present(row, _RATE_KEYS)
		if pay_rate is None:
			pay_rate = _first_present(shift, _RATE_KEYS)

		segments.append(
			_Segment(
				employee=employee,
				employee_name=row.get("employee_name") or shift.get("employee_name") or "",
				shift=shift_name,
				work_state=work_state,
				pay_type=pay_type,
				pay_rate=pay_rate,
				start=start,
				end=end,
				hours=hours,
				break_hours=paid_break,
				unpaid_break_hours=max(unpaid_break, 0.0),
				piece_units=_as_float(row.get("piece_units"), 0.0) + _piece_units_for(shift, employee),
				open_ended=open_ended,
			)
		)
	return segments


# ── Aggregation ───────────────────────────────────────────────────────────


def _week_index(when: date, anchor: date) -> int:
	return (when - anchor).days // WORKWEEK_DAYS


def _split_overtime(segments: list[_Segment], threshold: float) -> list[tuple]:
	"""Walk one workweek in time order; hours past `threshold` are overtime.

	Returns (segment, regular_hours, overtime_hours) per segment. A segment that
	straddles the fortieth hour is split across both — which is the honest answer
	for the shift on which somebody crossed it.
	"""
	out = []
	cumulative = 0.0
	for seg in segments:
		before = cumulative
		cumulative += seg.hours
		if before >= threshold:
			regular, overtime = 0.0, seg.hours
		elif cumulative <= threshold:
			regular, overtime = seg.hours, 0.0
		else:
			regular = threshold - before
			overtime = cumulative - threshold
		out.append((seg, regular, overtime))
	return out


def _bump(mapping: dict, key: str, amount: float) -> None:
	mapping[key] = round(mapping.get(key, 0.0) + amount, 4)


def aggregate_shifts_for_period(
	shifts: list[dict],
	pay_period_start,
	pay_period_end,
	overtime_threshold: float = OVERTIME_THRESHOLD_HOURS,
	workweek_anchor=None,
) -> dict:
	"""Per-employee timesheet totals for one pay period.

	Args:
		shifts: Shift dicts. Each may carry a `crew` list of rows with their own
			`joined_at`/`left_at`/`piece_units`, or name a single `employee`
			directly. `work_state`, `break_hours` (paid rest) and
			`unpaid_break_hours` (meal) may sit on the shift or be overridden per
			crew row. Piece units may arrive on the crew row, in a
			`piece_units_by_employee` map, or as rows in `piece_rows`, `tasks`,
			`bucket_logs` or `piece_logs`.
		pay_period_start: First day of the period, inclusive.
		pay_period_end: Last day of the period, inclusive.
		overtime_threshold: Hours in a workweek before the premium. 40 in both
			OR and WA; an argument so a stricter local rule can be expressed.
		workweek_anchor: First day of the employer's declared workweek. Defaults
			to `pay_period_start`, which is right whenever the period is a whole
			number of workweeks — and wrong, silently, when it is not, which is
			why it can be set.

	Returns:
		Dict keyed by employee docname. Each value carries total_hours,
		regular_hours, overtime_hours, break_hours, hours_by_state,
		overtime_hours_by_state, piece_units, piece_units_by_state, the weekly
		breakdown the overtime came out of, and the segments themselves.
	"""
	start = _as_date(pay_period_start)
	end = _as_date(pay_period_end)
	anchor = _as_date(workweek_anchor) or start

	by_employee: dict[str, dict] = {}
	outside: list[dict] = []

	for shift in shifts or []:
		for seg in _segments_of(shift):
			when = _as_date(seg.start)
			if start and end and when and not (start <= when <= end):
				outside.append(seg.as_dict())
				continue
			entry = by_employee.setdefault(seg.employee, {"segments": [], "name": ""})
			entry["segments"].append(seg)
			if seg.employee_name and not entry["name"]:
				entry["name"] = seg.employee_name

	aggregates: dict[str, dict] = {}
	for employee, entry in by_employee.items():
		segments = sorted(
			entry["segments"],
			key=lambda s: (s.start or datetime.min, s.shift or ""),
		)
		aggregates[employee] = _aggregate_one(
			employee,
			entry["name"],
			segments,
			anchor,
			start,
			overtime_threshold,
		)

	for agg in aggregates.values():
		agg["shifts_outside_period"] = [row for row in outside if row["employee"] == agg["employee"]]
	return aggregates


def _aggregate_one(
	employee: str,
	employee_name: str,
	segments: list[_Segment],
	anchor: date | None,
	period_start: date | None,
	threshold: float,
) -> dict:
	"""One employee's totals, with overtime resolved week by week."""
	weeks: dict[int, list[_Segment]] = {}
	for seg in segments:
		when = _as_date(seg.start)
		index = _week_index(when, anchor) if (when and anchor) else 0
		weeks.setdefault(index, []).append(seg)

	total_hours = regular_hours = overtime_hours = 0.0
	break_hours = unpaid_break_hours = piece_units = 0.0
	hours_by_state: dict[str, float] = {}
	overtime_by_state: dict[str, float] = {}
	break_hours_by_state: dict[str, float] = {}
	piece_units_by_state: dict[str, float] = {}
	week_rows: list[dict] = []
	open_shifts: list[str] = []
	detail: list[dict] = []
	pay_groups: dict[tuple, dict] = {}

	for index in sorted(weeks):
		members = weeks[index]
		week_hours = week_overtime = 0.0
		for seg, regular, overtime in _split_overtime(members, threshold):
			state = seg.work_state or ""
			total_hours += seg.hours
			regular_hours += regular
			overtime_hours += overtime
			break_hours += seg.break_hours
			unpaid_break_hours += seg.unpaid_break_hours
			piece_units += seg.piece_units
			week_hours += seg.hours
			week_overtime += overtime

			_bump(hours_by_state, state, seg.hours)
			_bump(overtime_by_state, state, overtime)
			_bump(break_hours_by_state, state, seg.break_hours)
			_bump(piece_units_by_state, state, seg.piece_units)

			# One group per (state, pay type, rate). The state because the tax
			# allocation and the wage floor are both per state; the pay type and
			# rate because a mixed day has to reach the engine as the two kinds of
			# work it was, not as one total with one method assumed over it.
			group_key = (state, seg.pay_type or "", seg.pay_rate)
			group = pay_groups.setdefault(
				group_key,
				{
					"work_state": state,
					"pay_type": seg.pay_type or "",
					"pay_rate": seg.pay_rate,
					"hours": 0.0,
					"overtime_hours": 0.0,
					"piece_units": 0.0,
					"break_hours": 0.0,
				},
			)
			group["hours"] += seg.hours
			group["overtime_hours"] += overtime
			group["piece_units"] += seg.piece_units
			group["break_hours"] += seg.break_hours

			if seg.open_ended and seg.shift:
				open_shifts.append(seg.shift)

			row = seg.as_dict()
			row["regular_hours"] = round(regular, 2)
			row["overtime_hours"] = round(overtime, 2)
			row["workweek"] = index + 1
			detail.append(row)

		week_start = anchor + timedelta(days=index * WORKWEEK_DAYS) if anchor else None
		week_rows.append(
			{
				"workweek": index + 1,
				"week_start": str(week_start) if week_start else None,
				"week_end": str(week_start + timedelta(days=WORKWEEK_DAYS - 1)) if week_start else None,
				"hours": round(week_hours, 2),
				"regular_hours": round(week_hours - week_overtime, 2),
				"overtime_hours": round(week_overtime, 2),
				"over_threshold": week_overtime > 0,
			}
		)

	return {
		"employee": employee,
		"employee_name": employee_name,
		"total_hours": round(total_hours, 2),
		"regular_hours": round(regular_hours, 2),
		"overtime_hours": round(overtime_hours, 2),
		"break_hours": round(break_hours, 2),
		"unpaid_break_hours": round(unpaid_break_hours, 2),
		"piece_units": round(piece_units, 2),
		"hours_by_state": {k: round(v, 2) for k, v in hours_by_state.items()},
		"overtime_hours_by_state": {k: round(v, 2) for k, v in overtime_by_state.items()},
		"break_hours_by_state": {k: round(v, 2) for k, v in break_hours_by_state.items()},
		"piece_units_by_state": {k: round(v, 2) for k, v in piece_units_by_state.items()},
		"pay_segments": [
			{
				"work_state": group["work_state"],
				"pay_type": group["pay_type"],
				"pay_rate": group["pay_rate"],
				"hours": round(group["hours"], 2),
				"overtime_hours": round(group["overtime_hours"], 2),
				"piece_units": round(group["piece_units"], 2),
				"break_hours": round(group["break_hours"], 2),
			}
			for group in pay_groups.values()
		],
		"mixed_pay_types": len({group["pay_type"] for group in pay_groups.values()}) > 1,
		"shift_count": len({seg.shift for seg in segments if seg.shift}),
		"segment_count": len(segments),
		"shifts": detail,
		"weeks": week_rows,
		"open_shifts": sorted(set(open_shifts)),
		"overtime_threshold": threshold,
		"shifts_outside_period": [],
	}


def empty_aggregate(employee: str, employee_name: str = "") -> dict:
	"""The aggregate of somebody who worked no shift in the period.

	Not the same as being left out. A salaried supervisor draws their salary on a
	week they filed no timesheet, and an hourly worker who was off draws zero —
	both of which are slips that should exist and say so.
	"""
	return _aggregate_one(employee, employee_name, [], None, None, OVERTIME_THRESHOLD_HOURS)


# ── Minimum wage, per state ───────────────────────────────────────────────


def check_minimum_wage_by_state(
	hours_by_state: dict,
	wages_by_state: dict,
	region_by_state: dict | None = None,
	min_wage_rates: dict | None = None,
	overtime_hours_by_state: dict | None = None,
) -> dict:
	"""Minimum wage tested state by state, with the shortfall priced.

	Tested per state rather than on the period total because the two rates are
	different — Washington's $16.66 against Oregon's $14.70 — and an average
	across both would let a compliant Washington week paper over an Oregon week
	that was not. The wage floor is a floor in the state where the work happened.

	THE FLOOR CARRIES THE OVERTIME PREMIUM. `regular × minimum + overtime ×
	minimum × 1.5`, not `hours × minimum`. Releases through v0.48.2 compared
	against the flat product, which passed a fifty-hour Oregon week at $735 when
	the statutory floor for it is $808.50 — the ten hours past forty are owed at
	$22.05 whatever the buckets came to. `overtime_hours_by_state` is how many of
	each state's hours were past the weekly threshold; omitting it tests the flat
	floor, which is right only for a week with no overtime in it.

	VERIFIES; the remedy is applied upstream. Since v0.49.0
	`payroll_calc.calculate_full_payroll` pays the floor — gross is the greater of
	what was earned and what the hours are owed — so on a slip computed by this
	app `shortfall` is zero and this function is the independent check that says
	so. A non-zero shortfall here means wages arrived from somewhere that did not
	apply the rule, which is exactly when somebody wants to be told.
	"""
	regions = region_by_state or {}
	overtime = overtime_hours_by_state or {}

	by_state = {}
	total_shortfall = 0.0
	failures = []
	for state, hours in (hours_by_state or {}).items():
		hours = _as_float(hours)
		if hours <= 0:
			continue
		region = regions.get(state, "standard")
		minimum = applicable_minimum_wage(state, region, min_wage_rates)
		state_ot = min(max(_as_float(overtime.get(state)), 0.0), hours)
		floor = minimum_wage_floor(hours, state_ot, minimum)
		wages = _as_float((wages_by_state or {}).get(state))
		effective = wages / hours
		short = max(floor - wages, 0.0)
		total_shortfall += short
		row = {
			"state": state,
			"region": region,
			"hours": round(hours, 2),
			"regular_hours": round(hours - state_ot, 2),
			"overtime_hours": round(state_ot, 2),
			"wages": round(wages, 2),
			"minimum_wage": minimum,
			"minimum_wage_floor": floor,
			"effective_hourly_rate": round(effective, 2),
			"meets_minimum_wage": short <= 0.005,
			"shortfall": round(short, 2),
		}
		by_state[state] = row
		if not row["meets_minimum_wage"]:
			failures.append(state)

	return {
		"by_state": by_state,
		"meets_minimum_wage": not failures,
		"states_below_minimum": failures,
		"total_shortfall": round(total_shortfall, 2),
	}


# ── Building the payroll engine's inputs ──────────────────────────────────


def _structure_for(salary_structures, employee: str) -> dict | None:
	"""A salary structure by employee, from a map or a list of them."""
	if not salary_structures:
		return None
	if isinstance(salary_structures, dict):
		found = salary_structures.get(employee)
		return dict(found) if found else None
	for row in salary_structures:
		if str(row.get("employee") or "") == employee:
			return dict(row)
	return None


def _w4_for(w4_data, employee: str) -> dict:
	"""One employee's W-4 out of a per-employee map, a single form, or nothing.

	A single W-4 dict applied to everybody is accepted because that is what a
	one-worker preview passes, and refusing it would make the simple call the
	awkward one. It is recognised by carrying `filing_status`.
	"""
	if isinstance(w4_data, dict):
		if "filing_status" in w4_data:
			return dict(w4_data)
		found = w4_data.get(employee)
		if found:
			return dict(found)
	return dict(DEFAULT_W4)


def _federal_table_for(federal_tax_tables, filing_status: str, checkbox: bool = False) -> list:
	"""Brackets for one filing status, from a per-status map or a flat list. v0.291.0: a W-4 with Step 2 checked
	takes the status's Step 2 checkbox schedule where the map carries one (key "<status>|Step 2 Checkbox")."""
	if not federal_tax_tables:
		return []
	if isinstance(federal_tax_tables, dict):
		if checkbox and federal_tax_tables.get(f"{filing_status}|Step 2 Checkbox"):
			return list(federal_tax_tables[f"{filing_status}|Step 2 Checkbox"])
		return list(federal_tax_tables.get(filing_status) or [])
	return list(federal_tax_tables)


def engine_shift_rows(aggregate: dict) -> list[dict]:
	"""The aggregate, restated as the per-state rows `payroll_calc` reads.

	One row per work state rather than one per shift, and that is the point: the
	overtime split has already been decided week by week and chronologically, and
	handing the engine the raw shifts back would invite it to decide again — on
	period totals, which is the arithmetic this module exists to replace.

	A row per state also preserves the cross-state allocation
	`calculate_full_payroll` needs: it splits gross by hours proportion, and the
	proportions are exactly these.

	v0.49.0. WHERE ANY SEGMENT NAMED ITS OWN PAY TYPE OR RATE, the rows are one per
	(state, pay type, rate) instead, each carrying that pay type and rate. That is
	how a mixed day survives the trip: six hours of picking and two of irrigation
	arrive as two rows, and the engine pays each by its own method rather than
	paying eight hours by whichever method the salary structure happens to name.
	Nothing changes for the ordinary day, where no row names anything and the rows
	are per state exactly as before.
	"""
	pay_segments = aggregate.get("pay_segments") or []
	if any(row.get("pay_type") or row.get("pay_rate") not in (None, "") for row in pay_segments):
		rows = []
		for group in pay_segments:
			row = {
				"work_state": group.get("work_state", ""),
				"hours": round(_as_float(group.get("hours")), 2),
				"overtime_hours": round(_as_float(group.get("overtime_hours")), 2),
				"piece_units": round(_as_float(group.get("piece_units")), 2),
				"break_hours": round(_as_float(group.get("break_hours")), 2),
			}
			if group.get("pay_type"):
				row["pay_type"] = group["pay_type"]
			if group.get("pay_rate") not in (None, ""):
				row["base_rate"] = _as_float(group["pay_rate"])
			rows.append(row)
		return rows

	hours_by_state = aggregate.get("hours_by_state") or {}
	if not hours_by_state:
		# No shifts, or shifts with no state on them. One stateless row keeps a
		# salaried employee's slip computable and an hourly employee's zero.
		return [
			{
				"work_state": "",
				"hours": aggregate.get("total_hours", 0.0),
				"overtime_hours": aggregate.get("overtime_hours", 0.0),
				"piece_units": aggregate.get("piece_units", 0.0),
				"break_hours": aggregate.get("break_hours", 0.0),
			}
		]

	overtime = aggregate.get("overtime_hours_by_state") or {}
	breaks = aggregate.get("break_hours_by_state") or {}
	units = aggregate.get("piece_units_by_state") or {}
	return [
		{
			"work_state": state,
			"hours": round(_as_float(hours), 2),
			"overtime_hours": round(_as_float(overtime.get(state)), 2),
			"piece_units": round(_as_float(units.get(state)), 2),
			"break_hours": round(_as_float(breaks.get(state)), 2),
		}
		for state, hours in hours_by_state.items()
	]


def build_payroll_inputs(
	employee_aggregates: dict,
	salary_structures,
	w4_data,
	state_configs: dict,
	fica_config: dict,
	federal_tax_tables=None,
	state_tax_tables: dict | None = None,
	oregon_withholding: dict | None = None,
	min_wage_rates: dict | None = None,
	pay_frequency: str = "Biweekly",
	ytd_by_employee: dict | None = None,
	deductions_by_employee: dict | None = None,
) -> list[dict]:
	"""Marry timesheet aggregates to structures and tax configuration.

	Returns one input dict per employee WHO HAS A SALARY STRUCTURE, each holding
	the four arguments `payroll_calc.calculate_full_payroll` takes plus the
	aggregate they came from. Employees without one are not in the list and are
	not silently zeroed — `employees_missing_structures` names them.

	Args:
		employee_aggregates: What `aggregate_shifts_for_period` returned.
		salary_structures: Per-employee map, or a list of structure dicts each
			carrying `employee`.
		w4_data: Per-employee map of W-4 dicts, or a single W-4 for everybody.
			Missing means `DEFAULT_W4` — single, no adjustments.
		state_configs: State code → State Tax Configuration dict.
		fica_config: FICA rates and wage bases.
		federal_tax_tables: Filing status → brackets, or a flat bracket list.
		state_tax_tables: State code → brackets.
		pay_frequency: Weekly, Biweekly, Semimonthly or Monthly.
		ytd_by_employee: Employee → {"ytd_gross", "ytd_ss_withheld"}. Absent is
			treated as zero, which is right for the first period of a year and
			understates the Social Security wage base consumed after it.
		deductions_by_employee: Employee → their Farm Payroll Deduction rows,
			already filtered to the ones in force in this period. Absent means
			no garnishment and no voluntary election, which is the ordinary
			worker and computes exactly as it did before deductions existed.

			PER EMPLOYEE AND NEVER SHARED, unlike the tax tables above it. A
			salary structure or a bracket table can sensibly be one object read
			by forty slips; a child support order cannot, and a bug that let one
			leak across employees would garnish the wrong person's wages.
	"""
	inputs = []
	for employee in sorted(employee_aggregates or {}):
		aggregate = employee_aggregates[employee]
		structure = _structure_for(salary_structures, employee)
		if not structure:
			continue

		w4 = _w4_for(w4_data, employee)
		ytd = (ytd_by_employee or {}).get(employee) or {}

		inputs.append(
			{
				"employee": employee,
				"employee_data": {
					"employee": employee,
					"employee_name": (
						aggregate.get("employee_name") or structure.get("employee_name") or employee
					),
				},
				"shifts": engine_shift_rows(aggregate),
				"salary_structure": {
					"name": structure.get("name", ""),
					"pay_type": structure.get("pay_type", "Hourly"),
					"base_rate": _as_float(structure.get("base_rate")),
					# What an hour of non-piece work is worth to a piece-rate
					# worker, and which region's floor applies to them. Both are
					# the engine's business now that it pays the floor rather
					# than reporting a distance from it.
					"hourly_rate": structure.get("hourly_rate"),
					"min_wage_regions": structure.get("min_wage_regions") or {},
				},
				"tax_config": {
					"w4_data": w4,
					"fica_config": dict(fica_config or {}),
					"federal_tax_table": _federal_table_for(
						federal_tax_tables,
						w4.get("filing_status", "Single"),
						checkbox=bool(w4.get("multiple_jobs")),
					),
					"pay_frequency": pay_frequency,
					"ytd_gross": _as_float(ytd.get("ytd_gross")),
					"ytd_ss_withheld": _as_float(ytd.get("ytd_ss_withheld")),
					"state_configs": dict(state_configs or {}),
					"state_tax_tables": dict(state_tax_tables or {}),
					# v0.291.0. Oregon's formula for this run (payroll_settings.oregon_withholding) and the pay year.
					"oregon_withholding": oregon_withholding,
					# v0.63.0. The wage floor for this run, off the site's own
					# State Tax Configurations. None means the engine falls back
					# to `payroll_calc.MINIMUM_WAGE_RATES`, which is what every
					# caller got before a table existed to configure.
					"min_wage_rates": dict(min_wage_rates or {}) or None,
				},
				"aggregate": aggregate,
				"min_wage_regions": structure.get("min_wage_regions") or {},
				"deductions": list((deductions_by_employee or {}).get(employee) or []),
			}
		)
	return inputs


def employees_missing_structures(employee_aggregates: dict, salary_structures) -> list[dict]:
	"""Everybody who worked in the period and has nobody to pay them.

	The whole reason this is a return value rather than a skipped iteration: a
	picker who worked ninety hours and whose structure was never created does not
	belong on a payroll run as a zero. They belong on a list somebody reads.
	"""
	missing = []
	for employee in sorted(employee_aggregates or {}):
		if _structure_for(salary_structures, employee):
			continue
		aggregate = employee_aggregates[employee]
		missing.append(
			{
				"employee": employee,
				"employee_name": aggregate.get("employee_name", ""),
				"total_hours": aggregate.get("total_hours", 0.0),
				"piece_units": aggregate.get("piece_units", 0.0),
				"shift_count": aggregate.get("shift_count", 0),
			}
		)
	return missing


# ── End to end ────────────────────────────────────────────────────────────


def _state_wages(slip: dict) -> dict:
	"""What each state's share of gross came to, from the engine's own split.

	`calculate_full_payroll` fills `state_hours[state]["gross"]` only on the
	cross-state path — a single-state period never needed the allocation — so the
	single-state case is filled in here rather than left at zero. A tax form
	reading `state_wages` should not have to know which branch produced the slip.
	"""
	state_hours = slip.get("state_hours") or {}
	gross = _as_float(slip.get("gross_pay"))
	if len(state_hours) == 1:
		return {next(iter(state_hours)): round(gross, 2)}
	return {state: round(_as_float(info.get("gross")), 2) for state, info in state_hours.items()}


def run_integrated_payroll(
	shifts: list[dict],
	salary_structures,
	w4_data,
	state_configs: dict,
	fica_config: dict,
	pay_period_start,
	pay_period_end,
	company: str = "",
	federal_tax_tables=None,
	state_tax_tables: dict | None = None,
	oregon_withholding: dict | None = None,
	min_wage_rates: dict | None = None,
	pay_frequency: str = "Biweekly",
	ytd_by_employee: dict | None = None,
	overtime_threshold: float = OVERTIME_THRESHOLD_HOURS,
	workweek_anchor=None,
	include_unworked: bool = True,
	deductions_by_employee: dict | None = None,
) -> list[dict]:
	"""Shifts in, payroll slips out.

	Aggregate → build inputs → calculate, for every employee at once. Each
	returned dict is what `payroll_calc.calculate_full_payroll` produced, plus
	the period, the company, the timesheet provenance the figure came from, and
	a per-state minimum wage verdict.

	`include_unworked` keeps an employee with a salary structure and no shift on
	the run — as a zero for hourly and piece rate, and as their salary for
	Salary. Turn it off for a run scoped to who actually worked.

	Returns:
		A list of slip dicts, ready to become Farm Payroll Slip rows. Sorted by
		employee so two runs of the same period produce the same order.
	"""
	aggregates = aggregate_shifts_for_period(
		shifts,
		pay_period_start,
		pay_period_end,
		overtime_threshold=overtime_threshold,
		workweek_anchor=workweek_anchor,
	)

	if include_unworked:
		for row in _structure_rows(salary_structures):
			employee = str(row.get("employee") or "")
			if employee and employee not in aggregates:
				aggregates[employee] = empty_aggregate(
					employee,
					row.get("employee_name") or "",
				)

	inputs = build_payroll_inputs(
		aggregates,
		salary_structures,
		w4_data,
		state_configs,
		fica_config,
		federal_tax_tables=federal_tax_tables,
		state_tax_tables=state_tax_tables,
		oregon_withholding=oregon_withholding,
		min_wage_rates=min_wage_rates,
		pay_frequency=pay_frequency,
		ytd_by_employee=ytd_by_employee,
		deductions_by_employee=deductions_by_employee,
	)

	slips = []
	for item in inputs:
		aggregate = item["aggregate"]
		slip = calculate_full_payroll(
			item["employee_data"],
			item["shifts"],
			item["salary_structure"],
			item["tax_config"],
			deductions=item.get("deductions"),
		)

		wages_by_state = _state_wages(slip)
		slip["state_wages"] = wages_by_state
		# The independent check, run on the wages as PAID — makeup included. It
		# should now find nothing, and the day it finds something is the day the
		# engine and the floor disagree about a slip somebody is about to be paid.
		slip["minimum_wage_detail"] = check_minimum_wage_by_state(
			aggregate.get("hours_by_state") or {},
			wages_by_state,
			item.get("min_wage_regions"),
			# v0.63.0. THE SAME TABLE THE ENGINE PAID FROM. Without this the
			# independent check would test against the SHIPPED rates while the
			# engine paid the site's configured ones — so a farm that raised its
			# own floor would see every topped-up slip flagged as below the
			# minimum, and a farm whose configured rate was lower would see
			# nothing flagged that should be. A cross-check reading a different
			# table from the thing it checks is not a cross-check.
			min_wage_rates=(item.get("tax_config") or {}).get("min_wage_rates"),
			overtime_hours_by_state=aggregate.get("overtime_hours_by_state") or {},
		)
		# What the floor added, on the slip and per state, so a report can show it
		# as its own line rather than leaving it inside gross where nobody can see
		# that a piece rate is set below what the hours are owed.
		makeup_by_state = {
			state: row.get("minimum_wage_makeup", 0.0)
			for state, row in (slip.get("minimum_wage_by_state") or {}).items()
			if row.get("minimum_wage_makeup", 0.0) > 0
		}
		slip["minimum_wage_makeup_by_state"] = makeup_by_state
		slip["minimum_wage_states_topped_up"] = sorted(makeup_by_state)
		for state, row in (slip.get("minimum_wage_by_state") or {}).items():
			detail_row = slip["minimum_wage_detail"]["by_state"].get(state)
			if detail_row is not None:
				detail_row["minimum_wage_makeup"] = row.get("minimum_wage_makeup", 0.0)
				detail_row["earned_wages"] = row.get("earned_wages", 0.0)
		slip["minimum_wage_detail"]["total_makeup"] = slip.get("minimum_wage_makeup", 0.0)
		slip["company"] = company
		slip["pay_period_start"] = str(pay_period_start)
		slip["pay_period_end"] = str(pay_period_end)
		slip["pay_frequency"] = pay_frequency
		slip["break_hours"] = aggregate.get("break_hours", 0.0)
		slip["unpaid_break_hours"] = aggregate.get("unpaid_break_hours", 0.0)
		slip["shift_count"] = aggregate.get("shift_count", 0)
		slip["hours_by_state"] = aggregate.get("hours_by_state", {})
		slip["overtime_hours_by_state"] = aggregate.get("overtime_hours_by_state", {})
		slip["weeks"] = aggregate.get("weeks", [])
		slip["timesheet"] = aggregate.get("shifts", [])
		slip["open_shifts"] = aggregate.get("open_shifts", [])
		slips.append(slip)

	return slips


def _structure_rows(salary_structures) -> list[dict]:
	"""Salary structures as a list of rows, whichever way they arrived."""
	if not salary_structures:
		return []
	if isinstance(salary_structures, dict):
		rows = []
		for employee, row in salary_structures.items():
			merged = dict(row or {})
			merged.setdefault("employee", employee)
			rows.append(merged)
		return rows
	return [dict(row) for row in salary_structures]


def summarize_payroll_run(slips: list[dict]) -> dict:
	"""Run totals, and the two lists somebody has to look at before paying.

	`below_minimum_wage` and `with_open_shifts` are the exceptions: a state whose
	hours did not clear its floor, and a shift that never got an end time so its
	hours counted as zero. Both produce a slip that computes cleanly and is
	wrong, which is the only kind of wrong worth putting at the top of a summary.

	v0.49.0 adds a third and it is the one to read: `topped_up_to_minimum_wage`,
	everybody whose piece earnings did not reach what their hours are owed and
	whose gross was raised to the floor. Nobody on that list is underpaid — that
	is the whole change — but every name on it is a rate set too low to be lawful
	on the work that was actually done, and `below_minimum_wage` no longer catches
	them because the makeup already did.
	"""
	total_gross = total_deductions = total_net = 0.0
	total_hours = total_overtime = total_units = 0.0
	total_makeup = 0.0
	total_garnishments = total_pre_tax = total_post_tax = 0.0
	below = []
	topped_up = []
	open_shifts = []
	deduction_shortfalls = []
	for slip in slips or []:
		total_garnishments += _as_float(slip.get("garnishment_total"))
		total_pre_tax += _as_float(slip.get("pre_tax_deductions"))
		total_post_tax += _as_float(slip.get("post_tax_deductions"))
		if slip.get("deduction_shortfalls"):
			deduction_shortfalls.append(
				{
					"employee": slip.get("employee"),
					"employee_name": slip.get("employee_name"),
					"shortfall": round(
						sum(_as_float(row.get("shortfall")) for row in slip["deduction_shortfalls"]), 2
					),
					"lines": [
						{
							"label": row.get("label"),
							"deduction_category": row.get("deduction_category"),
							"requested": row.get("requested"),
							"withheld": row.get("amount"),
							"shortfall": row.get("shortfall"),
							"reason": row.get("shortfall_reason", ""),
						}
						for row in slip["deduction_shortfalls"]
					],
				}
			)
		total_gross += _as_float(slip.get("gross_pay"))
		makeup = _as_float(slip.get("minimum_wage_makeup"))
		total_makeup += makeup
		if makeup > 0:
			topped_up.append(
				{
					"employee": slip.get("employee"),
					"employee_name": slip.get("employee_name"),
					"states": slip.get("minimum_wage_states_topped_up") or [],
					"earned_gross": _as_float(slip.get("earned_gross")),
					"minimum_wage_makeup": round(makeup, 2),
					"gross_pay": _as_float(slip.get("gross_pay")),
				}
			)
		total_deductions += _as_float(slip.get("total_deductions"))
		total_net += _as_float(slip.get("net_pay"))
		total_hours += _as_float(slip.get("total_hours"))
		total_overtime += _as_float(slip.get("overtime_hours"))
		total_units += _as_float(slip.get("piece_units"))
		detail = slip.get("minimum_wage_detail") or {}
		if detail.get("states_below_minimum"):
			below.append(
				{
					"employee": slip.get("employee"),
					"employee_name": slip.get("employee_name"),
					"states": detail["states_below_minimum"],
					"shortfall": detail.get("total_shortfall", 0.0),
				}
			)
		if slip.get("open_shifts"):
			open_shifts.append(
				{
					"employee": slip.get("employee"),
					"shifts": slip["open_shifts"],
				}
			)

	return {
		"employee_count": len(slips or []),
		"total_gross": round(total_gross, 2),
		"total_deductions": round(total_deductions, 2),
		"total_net": round(total_net, 2),
		"total_hours": round(total_hours, 2),
		"total_overtime_hours": round(total_overtime, 2),
		"total_piece_units": round(total_units, 2),
		"total_minimum_wage_makeup": round(total_makeup, 2),
		"total_garnishments": round(total_garnishments, 2),
		"total_pre_tax_deductions": round(total_pre_tax, 2),
		"total_post_tax_deductions": round(total_post_tax, 2),
		"below_minimum_wage": below,
		"topped_up_to_minimum_wage": topped_up,
		"with_open_shifts": open_shifts,
		# The fourth list, and it belongs beside the other three for the same
		# reason: a slip that could not satisfy a court order computes cleanly
		# and is a liability. An employer who withholds less than an order
		# demands answers to the court for the difference, and nothing carries
		# the balance forward — so this is the only place it is ever said.
		"deduction_shortfalls": deduction_shortfalls,
	}

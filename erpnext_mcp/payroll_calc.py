# SPDX-License-Identifier: MIT
"""Payroll calculation engine — PURE FUNCTIONS.

No database reads, no side effects, fully testable with deterministic inputs.

v0.30.0. Ties the federal withholding engine (v0.28.0) and the state tax
engines (v0.29.0) into actual payroll: gross pay from hours and piece units,
overtime at 1.5x, break pay at the average piece-rate hourly, minimum wage
checks, and the full deduction stack.

MINIMUM WAGE (2025):
  Oregon: $14.70/hr standard, $13.70 non-urban, $15.95 Portland metro
  Washington: $16.66/hr

OVERTIME (both states, ag workers, fully phased):
  Oregon HB 4002: 40 hrs/wk threshold
  Washington SB 5172: 40 hrs/wk threshold
  1.5x the regular rate

────────────────────────────────────────────────────────────────────────────
v0.49.0 — THE HIGHER-OF RULE, AND THREE THINGS THAT FOLLOW FROM IT
────────────────────────────────────────────────────────────────────────────

THE FLOOR IS NOW PAID, NOT JUST PRICED. FLSA §6 and both state schemes make the
minimum wage a floor under the WAGE, not a figure to compare the wage against:
piece rate is a method of measuring pay, never a way to earn less than the hours
were worth. So gross pay is `max(what was earned, what the hours are owed)`, and
the difference is carried as `minimum_wage_makeup` — its own figure on the slip,
its own column on the stored row, its own line in the run summary. Forty-seven
buckets at $1.50 in an eight-hour Oregon day now pays $117.60, of which $47.10 is
makeup, and the makeup is the number that says the piece rate is set too low.

Paying it does not hide it. Every prior release reported the shortfall and left
gross alone precisely so a rate below the lawful floor would stay visible; the
makeup figure keeps that visibility and stops the farm owing back wages while it
is visible.

THE FLOOR KNOWS ABOUT OVERTIME. `regular_hours × minimum + overtime_hours ×
minimum × 1.5`, not `total_hours × minimum`. Fifty hours in Oregon is a floor of
$808.50, not $735 — the ten overtime hours are owed at $22.05 whatever the
buckets came to.

THE FLOOR IS PER STATE. A worker with Oregon hours and Washington hours has two
floors, each over its own hours, and the makeup is computed and reported for each.
Averaging them would let a Washington week paper over an Oregon one.

THE OVERTIME PREMIUM ON PIECE WORK IS THE FLSA HALF-TIME PREMIUM. 29 CFR 778.111:
the regular rate is all straight-time earnings divided by all hours worked, and
because the piece earnings ALREADY paid straight time for the overtime hours, what
is owed on top is one HALF the regular rate, not one and a half. Releases through
v0.48.2 paid the full 1.5x on top — more than the law asks, and out of step with
the mixed-rate arithmetic below, where a weighted average is the only method that
works. Both now use half-time.

A WORKER CAN BE PART PIECE-RATE AND PART HOURLY. `calculate_mixed_gross_pay`
takes the day as segments — six hours picking, two hours on irrigation — pays each
by its own method, and computes one regular rate across the lot the way 29 CFR
778.115 says to for an employee working at two rates in one workweek. Six hours of
buckets and two of irrigation is $167, and the minimum wage floor is tested on all
eight hours.
"""

from __future__ import annotations

from . import payroll_deductions
from .state_withholding import calculate_state_withholding
from .withholding import calculate_federal_withholding

#: THE SHIPPED FLOOR, AND THE FALLBACK RATHER THAN THE ANSWER. v0.63.0: a State
#: Tax Configuration may now carry its own three rates per company, state and tax
#: year, and `state_min_wage_rates` merges those over this table — so a rate
#: change is a row somebody edits in the Desk instead of a release. This stays as
#: the default because a site that has configured nothing must still pay a lawful
#: floor: a table that started empty would make the higher-of rule a no-op on
#: every install until an operator noticed, and "the minimum wage was zero" is
#: the one wrong answer in this module that nobody would see on a slip.
MINIMUM_WAGE_RATES = {
	"OR": {"standard": 14.70, "non_urban": 13.70, "portland_metro": 15.95},
	"WA": {"standard": 16.66},
}

#: `min_wage_region` on a Farm Salary Structure → the key in the table above.
#: The Select is written the way a person reads it and the table is keyed the way
#: a dict is; one mapping rather than two spellings kept in step by hand.
MIN_WAGE_REGION_KEYS = {
	"standard": "standard",
	"non-urban": "non_urban",
	"non urban": "non_urban",
	"non_urban": "non_urban",
	"nonurban": "non_urban",
	"portland metro": "portland_metro",
	"portland-metro": "portland_metro",
	"portland_metro": "portland_metro",
	"portland": "portland_metro",
	"metro": "portland_metro",
}

#: The column on a State Tax Configuration that carries each region's rate.
MIN_WAGE_COLUMNS = {
	"standard": "minimum_wage",
	"non_urban": "minimum_wage_non_urban",
	"portland_metro": "minimum_wage_portland_metro",
}


def normalise_min_wage_region(value) -> str:
	"""`"Portland Metro"` → `"portland_metro"`. Unknown or empty → `"standard"`.

	FALLS BACK RATHER THAN RAISING, because the caller is a payroll run and the
	value came off a Select on somebody's salary structure. A region this app does
	not recognise is a data problem worth one wrong-but-lawful floor — the standard
	rate, which is the one every worker in the state is owed at minimum — and not
	worth refusing to pay a whole company over.
	"""
	raw = str(value or "").strip().casefold()
	if not raw:
		return "standard"
	return MIN_WAGE_REGION_KEYS.get(raw, "standard")


def state_min_wage_rates(state_configs: dict, shipped: dict | None = None) -> dict:
	"""The floor table for a run: the shipped rates with this site's own over them.

	Args:
		state_configs: State code → State Tax Configuration as a dict, which is
			what `_load_state_configs` already builds for the withholding
			engines. Read here rather than fetched again, so the row that says
			what SUTA costs and the row that says what an hour is worth cannot
			come from two different configurations of the same state.
		shipped: The defaults to merge over. `MINIMUM_WAGE_RATES` unless a test
			passes its own.

	Returns:
		A new dict in `MINIMUM_WAGE_RATES`' shape. The input is not modified.

	ZERO MEANS "NOT SET HERE", NOT "THE FLOOR IS ZERO". Currency fields default to
	0 on every row of every doctype, so treating a zero as an override would let a
	site that filled in nothing but its SUTA rate silently drop the wage floor to
	nothing for its whole payroll — and the makeup that catches an underpaid piece
	rate would compute against it and find nothing wrong. A rate has to be a
	positive number to win.

	A STATE WITH A CONFIGURATION AND NO RATES KEEPS THE SHIPPED ONES, which is what
	every site upgrading to v0.63.0 is: the columns are new and empty everywhere,
	and the release must not change what a single slip pays until somebody types a
	number into one.
	"""
	rates = {state: dict(regions) for state, regions in (shipped or MINIMUM_WAGE_RATES).items()}
	for state, config in (state_configs or {}).items():
		if not isinstance(config, dict):
			continue
		for region, column in MIN_WAGE_COLUMNS.items():
			try:
				value = float(config.get(column) or 0.0)
			except (TypeError, ValueError):
				continue
			if value > 0:
				rates.setdefault(state, {})[region] = value
	return rates


#: What an overtime hour is worth in total: one and a half times the regular rate.
OT_MULTIPLIER = 1.5

#: What is owed ON TOP where straight time for the overtime hours has already been
#: paid — by the piece, or by an hourly segment that counted every hour. 29 CFR
#: 778.111 (piece rates) and 778.115 (two or more rates in one workweek): the
#: premium is half the regular rate, because the other one is already in hand.
OT_PREMIUM_MULTIPLIER = OT_MULTIPLIER - 1.0

def _multiplier() -> float:
	"""The overtime multiplier in force (v0.235.0: `payroll_settings`, built-in 1.5 until a
	version is published — so nothing changes until a person publishes one)."""
	try:
		from . import payroll_settings

		return payroll_settings.ot_multiplier()
	except Exception:
		return OT_MULTIPLIER


def _premium_multiplier() -> float:
	try:
		from . import payroll_settings

		return payroll_settings.ot_premium_multiplier()
	except Exception:
		return OT_PREMIUM_MULTIPLIER


#: The pay types a segment or a structure can name. "Salary" is period pay and has
#: no per-hour or per-unit reading, so it never takes part in a mixed day.
PAY_TYPES = ("Piece Rate", "Hourly", "Salary")


def calculate_gross_pay(
	pay_type: str,
	base_rate: float,
	hours: float,
	overtime_hours: float,
	piece_units: float,
	break_hours: float = 0.0,
) -> dict:
	"""Calculate gross pay from raw inputs.

	For piece rate workers: gross = (units * rate) + break_pay + OT_pay.
	For hourly workers: gross = (regular_hours * rate) + (OT_hours * rate * 1.5).
	For salary: gross = base_rate (the periodic salary amount).

	Returns a dict with gross_pay and the components that built it.
	"""
	regular_hours = max(hours - overtime_hours, 0)

	if pay_type == "Piece Rate":
		piece_earnings = piece_units * base_rate
		piece_hours = hours - break_hours
		bp = calculate_break_pay(piece_earnings, piece_hours, break_hours)
		# The FLSA regular rate: everything earned at straight time over every hour
		# worked. Written as `piece_earnings / piece_hours` because that is
		# identically `(piece_earnings + break_pay) / (piece_hours + break_hours)`
		# — the break was paid at this very rate — and the short form does not
		# divide by zero on a day that was all break.
		effective_rate = (piece_earnings / piece_hours) if piece_hours > 0 else 0.0
		premium = _premium_multiplier()
		ot = calculate_overtime(
			0,
			overtime_hours,
			effective_rate,
			multiplier=premium,
		)
		gross = piece_earnings + bp + ot
		return {
			"gross_pay": round(gross, 2),
			"piece_earnings": round(piece_earnings, 2),
			"break_pay": round(bp, 2),
			"overtime_pay": round(ot, 2),
			"effective_hourly_rate": round(effective_rate, 2),
			"regular_hours": round(regular_hours, 2),
			"overtime_hours": round(overtime_hours, 2),
			"piece_units": piece_units,
			"piece_rate": base_rate,
			"overtime_premium_multiplier": premium,
			"pay_type": "Piece Rate",
		}

	elif pay_type == "Hourly":
		regular_pay = regular_hours * base_rate
		ot = calculate_overtime(0, overtime_hours, base_rate)
		gross = regular_pay + ot
		return {
			"gross_pay": round(gross, 2),
			"regular_pay": round(regular_pay, 2),
			"overtime_pay": round(ot, 2),
			"effective_hourly_rate": round(base_rate, 2),
			"regular_hours": round(regular_hours, 2),
			"overtime_hours": round(overtime_hours, 2),
			"pay_type": "Hourly",
		}

	else:  # Salary
		return {
			"gross_pay": round(base_rate, 2),
			"effective_hourly_rate": round(base_rate / hours, 2) if hours > 0 else 0.0,
			"regular_hours": round(hours, 2),
			"overtime_hours": 0.0,
			"pay_type": "Salary",
		}


def calculate_break_pay(
	piece_earnings: float,
	piece_hours: float,
	break_hours: float = 0.0,
) -> float:
	"""Break pay at the average piece-rate hourly.

	WA WAC 296-131-020 and OR similar: rest breaks for piece-rate workers
	are paid at the average hourly rate earned during the piece-rate period.
	"""
	if piece_hours <= 0 or break_hours <= 0:
		return 0.0
	avg_rate = piece_earnings / piece_hours
	return round(avg_rate * break_hours, 2)


def calculate_overtime(
	regular_pay: float,
	overtime_hours: float,
	effective_rate: float,
	multiplier: float | None = None,
) -> float:
	"""Overtime pay at `multiplier` times the effective rate.

	Both OR (HB 4002) and WA (SB 5172) use a 40-hour threshold for ag
	workers, fully phased. The caller is responsible for splitting hours
	into regular and overtime at the 40-hour boundary.

	The multiplier is the caller's because it depends on what has already been
	paid for those hours. An hourly worker whose regular pay covered only the
	regular hours is owed the whole 1.5x on the overtime ones — the default. A
	piece-rate worker whose piece earnings already paid straight time for every
	hour is owed the HALF-TIME premium on top, `OT_PREMIUM_MULTIPLIER`, which is
	what 29 CFR 778.111 asks for. Both arrive at the same place; the difference is
	only which half was already in the number.
	"""
	if multiplier is None:
		multiplier = _multiplier()
	if overtime_hours <= 0:
		return 0.0
	return round(overtime_hours * effective_rate * multiplier, 2)


def calculate_mixed_gross_pay(
	segments: list[dict],
	overtime_hours: float = 0.0,
) -> dict:
	"""Gross pay for a period whose segments were not all paid the same way.

	THE DAY THIS EXISTS FOR: six hours picking at $1.50 a bucket and two hours on
	irrigation at $16.00. `calculate_gross_pay` branches on ONE pay type for a
	whole slip, so under a piece-rate structure those two irrigation hours earned
	nothing — they produced no buckets — and under an hourly structure the ninety
	buckets earned nothing. Neither is what the worker did.

	THE METHOD IS 29 CFR 778.115, the one the FLSA gives for an employee working
	at two or more rates in one workweek: pay each segment by its own method at
	straight time, then take the REGULAR RATE as the whole lot divided by all the
	hours worked, and pay the overtime premium at half that rate for the hours past
	the threshold. The alternative — a premium per segment at that segment's own
	rate — needs an agreement under 778.419 and is not what a farm has.

	Args:
		segments: One dict per stretch of work, each with `pay_type` ("Piece Rate"
			or "Hourly"), `rate` (per unit or per hour, respectively), `hours`, and
			optionally `piece_units`, `break_hours` (paid rest) and `work_state`.
		overtime_hours: Hours in the period past the weekly threshold, already
			resolved week by week by the caller. NOT re-derived here: a pay period
			is not a workweek and this function cannot see the calendar.

	Returns:
		The same shape `calculate_gross_pay` returns, plus `hourly_earnings` and a
		`segments` list carrying what each stretch came to.
	"""
	rows = []
	piece_earnings = hourly_earnings = break_pay = 0.0
	total_hours = piece_units = 0.0

	for segment in segments or []:
		seg_type = segment.get("pay_type") or "Hourly"
		rate = float(segment.get("rate") or 0.0)
		hours = float(segment.get("hours") or 0.0)
		breaks = min(max(float(segment.get("break_hours") or 0.0), 0.0), max(hours, 0.0))
		units = float(segment.get("piece_units") or 0.0)

		if seg_type == "Piece Rate":
			earned = units * rate
			worked_hours = hours - breaks
			rest = calculate_break_pay(earned, worked_hours, breaks)
			piece_earnings += earned
			break_pay += rest
			piece_units += units
			straight = earned + rest
		else:
			# Every hour of an hourly segment, overtime ones included. The premium
			# for those is added once at the end, at the regular rate across all
			# segments — adding 1.5x here would pay it twice and at the wrong rate.
			earned = hours * rate
			hourly_earnings += earned
			straight = earned

		total_hours += hours
		rows.append(
			{
				"pay_type": seg_type,
				"rate": rate,
				"hours": round(hours, 2),
				"break_hours": round(breaks, 2),
				"piece_units": round(units, 2) if seg_type == "Piece Rate" else 0.0,
				"work_state": segment.get("work_state", ""),
				"straight_time_pay": round(straight, 2),
			}
		)

	straight_time = piece_earnings + hourly_earnings + break_pay
	regular_rate = (straight_time / total_hours) if total_hours > 0 else 0.0
	overtime_hours = max(float(overtime_hours or 0.0), 0.0)
	ot = calculate_overtime(
		0,
		overtime_hours,
		regular_rate,
		multiplier=_premium_multiplier(),
	)

	kinds = {row["pay_type"] for row in rows}
	return {
		"gross_pay": round(straight_time + ot, 2),
		"piece_earnings": round(piece_earnings, 2),
		"hourly_earnings": round(hourly_earnings, 2),
		"piece_rate": next(
			(row["rate"] for row in rows if row["pay_type"] == "Piece Rate"),
			0.0,
		),
		"straight_time_pay": round(straight_time, 2),
		"break_pay": round(break_pay, 2),
		"overtime_pay": round(ot, 2),
		"effective_hourly_rate": round(regular_rate, 2),
		"regular_hours": round(max(total_hours - overtime_hours, 0.0), 2),
		"overtime_hours": round(overtime_hours, 2),
		"piece_units": round(piece_units, 2),
		"overtime_premium_multiplier": _premium_multiplier(),
		# "Mixed" only where it actually was. A day of segments that all turned out
		# to be piece work is a piece-rate day, and calling it mixed on a slip
		# somebody reads would be describing the code path rather than the work.
		"pay_type": kinds.pop() if len(kinds) == 1 else "Mixed",
		"segments": rows,
	}


def minimum_wage_floor(
	hours: float,
	overtime_hours: float,
	minimum_wage: float,
) -> float:
	"""What the hours are owed at the floor, overtime premium included.

	`regular × minimum + overtime × minimum × 1.5`, and NOT `total × minimum`.
	The statutory floor for somebody who worked fifty hours is not fifty hours of
	minimum wage: the ten past forty are owed at one and a half of it, whatever
	pay method produced the earnings being tested against it. Oregon HB 4002 and
	Washington SB 5172 both put the ag threshold at forty in a workweek; FLSA §7
	is the same arithmetic federally.
	"""
	hours = max(float(hours or 0.0), 0.0)
	overtime_hours = min(max(float(overtime_hours or 0.0), 0.0), hours)
	regular_hours = hours - overtime_hours
	return round(
		regular_hours * minimum_wage + overtime_hours * minimum_wage * _multiplier(),
		2,
	)


def applicable_minimum_wage(
	state: str,
	region: str = "standard",
	min_wage_rates: dict | None = None,
) -> float:
	"""The floor for one state and region, or zero where none is named.

	Zero for an unknown state is deliberate and is the same answer
	`check_minimum_wage_by_state` gives: a shift with no work state on it has no
	legislature behind it, and inventing a floor would be reporting a violation of
	a law nobody named — or, now that the floor is PAID, topping somebody's pay up
	to a number this app made up.
	"""
	rates = min_wage_rates or MINIMUM_WAGE_RATES
	state_rates = rates.get(state, {})
	return state_rates.get(region, state_rates.get("standard", 0.0))


def check_minimum_wage(
	gross_pay: float,
	total_hours: float,
	state: str,
	min_wage_rates: dict | None = None,
	region: str = "standard",
	overtime_hours: float = 0.0,
) -> dict:
	"""Check pay against the floor, and price what it would take to reach it.

	v0.49.0 changed this from a verdict into a verdict AND a figure, because the
	figure is what `calculate_full_payroll` now pays. `minimum_wage_makeup` is
	`max(0, floor − gross)` — zero on a compliant slip, and on any other one the
	amount that has to be added to gross for the hours to have been lawfully paid.

	The floor is `minimum_wage_floor`, which carries the overtime premium. Passing
	`overtime_hours` matters: fifty Oregon hours are owed $808.50, not $735, and a
	check that compared against the flat number would pass a slip that is $73.50
	short of the law.

	Args:
		gross_pay: What the period actually pays, before this makeup.
		total_hours: Hours worked, paid rest included, unpaid meal excluded.
		state: Two-letter code. One not in the table has no floor.
		min_wage_rates: Override the shipped table — a rate change lands here.
		region: "standard", "non_urban" or "portland_metro" in Oregon.
		overtime_hours: Of `total_hours`, how many were past the weekly threshold.

	Returns:
		Dict with meets_minimum_wage (bool), effective_hourly_rate,
		minimum_wage for the state and region, the minimum_wage_floor those
		hours are owed, and the minimum_wage_makeup that closes the gap.
	"""
	min_wage = applicable_minimum_wage(state, region, min_wage_rates)
	overtime_hours = min(max(float(overtime_hours or 0.0), 0.0), max(total_hours, 0.0))
	floor = minimum_wage_floor(total_hours, overtime_hours, min_wage)

	if total_hours <= 0:
		return {
			"meets_minimum_wage": True,
			"effective_hourly_rate": 0.0,
			"minimum_wage": min_wage,
			"minimum_wage_floor": 0.0,
			"minimum_wage_makeup": 0.0,
			"regular_hours": 0.0,
			"overtime_hours": 0.0,
			"state": state,
			"region": region,
		}

	makeup = round(max(floor - gross_pay, 0.0), 2)
	effective_rate = gross_pay / total_hours
	return {
		# A cent of rounding is not a wage violation, and `floor - gross` on a slip
		# that was topped up to the floor can land a hair either side of zero.
		"meets_minimum_wage": makeup <= 0.005,
		"effective_hourly_rate": round(effective_rate, 2),
		"minimum_wage": min_wage,
		"minimum_wage_floor": floor,
		"minimum_wage_makeup": makeup,
		"regular_hours": round(total_hours - overtime_hours, 2),
		"overtime_hours": round(overtime_hours, 2),
		"state": state,
		"region": region,
	}


def _segment_rate(
	row: dict,
	row_pay_type: str,
	structure_pay_type: str,
	base_rate: float,
	hourly_rate=None,
) -> float:
	"""What one stretch of work is paid at, per unit or per hour.

	Three places to look, in this order, and the order is the whole content of
	this function: the ROW's own rate, because a shift that says what it paid is
	the most specific record there is; the structure's `hourly_rate`, which is what
	a piece-rate worker's non-piece hours are worth and the field that exists for
	exactly this; and the structure's `base_rate` where the segment is paid the
	structure's own way after all.

	A segment that matches none of them is rated at ZERO rather than at the
	structure's rate, because the structure's rate is per BUCKET and paying
	irrigation hours at $1.50 an hour would be worse than paying nothing — it would
	look deliberate. Zero is loud, and the minimum wage makeup catches it: those
	hours are still owed the floor, so the worker is paid and the run reports a
	makeup that says a rate is missing.
	"""
	for key in ("base_rate", "pay_rate", "rate", "hourly_rate"):
		if row.get(key) not in (None, ""):
			try:
				return float(row[key])
			except (TypeError, ValueError):
				break
	if row_pay_type == structure_pay_type:
		return base_rate
	if row_pay_type == "Hourly" and hourly_rate not in (None, ""):
		try:
			return float(hourly_rate)
		except (TypeError, ValueError):
			return 0.0
	return 0.0


def calculate_full_payroll(
	employee_data: dict,
	shifts: list[dict],
	salary_structure: dict,
	tax_config: dict,
	deductions: list[dict] | None = None,
) -> dict:
	"""Orchestrate a complete payroll slip for one employee.

	Aggregates shifts by state, calculates gross pay, runs the federal
	engine, FICA, and the appropriate state engine based on work_state.

	Args:
		employee_data: Dict with employee, employee_name, etc.
		shifts: List of shift dicts with work_state, hours, overtime_hours,
			piece_units, break_hours. A row may also carry its OWN `pay_type` and
			`base_rate`, which is how a mixed day arrives — six hours of picking
			and two of irrigation are two rows with two pay types, and the slip is
			computed by `calculate_mixed_gross_pay` rather than by one branch.
		salary_structure: Dict with pay_type, base_rate, name (docname), and
			optionally `hourly_rate` (what non-piece hours pay under a piece-rate
			structure) and `min_wage_regions` ({"OR": "portland_metro"}).
		tax_config: Dict with w4_data, fica_config, federal_tax_table,
			pay_frequency, ytd_gross, ytd_ss_withheld, and per-state
			state_configs keyed by state code (e.g. {"OR": {...}, "WA": {...}})
			and state_tax_tables keyed by state code.
		deductions: Farm Payroll Deduction rows for this employee, already
			filtered to the ones in force in the period. None or empty is the
			ordinary slip and computes exactly as every release before this one
			— the whole deduction stack collapses to zero and no figure moves.

			GARNISHMENTS AND VOLUNTARY DEDUCTIONS, and the order between them is
			`payroll_deductions`' business rather than this function's. What
			happens HERE is the half that cannot live in a pure deduction module
			because it changes the tax base: pre-tax elections are subtracted
			BEFORE the withholding engines run, and they are subtracted twice
			over at two different figures, because a 401(k) leaves the income tax
			base and stays in the FICA one while a Section 125 premium leaves
			both. See `payroll_deductions.calculate_pre_tax_deductions`.

	Returns:
		Complete slip dict with gross, all deductions, and net.
	"""
	pay_type = salary_structure.get("pay_type", "Hourly")
	base_rate = float(salary_structure.get("base_rate", 0))
	hourly_rate = salary_structure.get("hourly_rate")
	min_wage_regions = salary_structure.get("min_wage_regions") or {}
	min_wage_rates = tax_config.get("min_wage_rates") or MINIMUM_WAGE_RATES
	pay_frequency = tax_config.get("pay_frequency", "Biweekly")

	# ── Aggregate shift data ──────────────────────────────────────────
	total_hours = 0.0
	regular_hours = 0.0
	overtime_hours = 0.0
	piece_units = 0.0
	break_hours = 0.0
	state_hours = {}
	segments = []
	mixed = False

	for shift in shifts:
		h = float(shift.get("hours", 0))
		ot = float(shift.get("overtime_hours", 0))
		units = float(shift.get("piece_units", 0))
		breaks = float(shift.get("break_hours", 0))
		total_hours += h
		overtime_hours += ot
		regular_hours += max(h - ot, 0)
		piece_units += units
		break_hours += breaks

		ws = shift.get("work_state", "")
		if ws:
			entry = state_hours.setdefault(
				ws,
				{"hours": 0.0, "overtime_hours": 0.0, "gross": 0.0},
			)
			entry["hours"] += h
			entry["overtime_hours"] += ot

		# A row that names its own pay type is a stretch of work paid a different
		# way from the rest of the structure. One such row anywhere makes the whole
		# slip a mixed one, because the regular rate is a property of the WEEK and
		# cannot be computed a segment at a time.
		row_type = shift.get("pay_type") or pay_type
		if shift.get("pay_type") or shift.get("base_rate") not in (None, ""):
			mixed = True
		segments.append(
			{
				"pay_type": row_type,
				"rate": _segment_rate(shift, row_type, pay_type, base_rate, hourly_rate),
				"hours": h,
				"piece_units": units,
				"break_hours": breaks,
				"work_state": ws,
			}
		)

	# ── Gross pay ─────────────────────────────────────────────────────
	#
	# Salary is period pay: it has no per-hour or per-unit reading, so a segment
	# claiming otherwise cannot be honoured and the structure wins.
	if mixed and pay_type != "Salary":
		gross_result = calculate_mixed_gross_pay(segments, overtime_hours)
	else:
		gross_result = calculate_gross_pay(
			pay_type,
			base_rate,
			total_hours,
			overtime_hours,
			piece_units,
			break_hours,
		)
	earned_gross = gross_result["gross_pay"]

	# ── Determine primary work state ─────────────────────────────────
	primary_state = ""
	if state_hours:
		primary_state = max(state_hours, key=lambda s: state_hours[s]["hours"])

	# ── The higher-of rule ────────────────────────────────────────────
	#
	# FLSA §6, ORS 653.025 and RCW 49.46.020: the minimum wage is a floor under
	# the wage itself. Piece rate is a way of MEASURING pay, never a way to earn
	# less than the hours were worth, so gross is the greater of what was earned
	# and what the hours are owed — and the difference is carried as its own
	# figure rather than folded silently into gross.
	#
	# Per state, because the floors differ and each one covers its own hours.
	# Washington's $16.66 over the Washington hours and Oregon's $14.70 over the
	# Oregon ones; one average across both would let a compliant week in one state
	# pay for a short one in the other.
	#
	# NOT ON A SALARY STRUCTURE, and that is the one place the old posture is kept
	# on purpose. Whether a salaried employee is exempt from the minimum wage at
	# all — executive, administrative, professional, or one of the agricultural
	# exemptions — is a fact about their job that this app does not hold, and
	# raising an exempt supervisor's pay because a sixty-hour harvest week divided
	# their salary below $14.70 would be inventing an obligation. So a salaried
	# shortfall is computed and REPORTED, exactly as every shortfall was before
	# v0.49.0, and somebody who knows the answer decides. Piece rate and hourly
	# carry no such question: those hours are owed the floor.
	applies = pay_type != "Salary"
	minimum_wage_by_state = {}
	total_makeup = 0.0
	total_floor = 0.0
	for state, info in state_hours.items():
		if total_hours <= 0:
			continue
		region = min_wage_regions.get(state, "standard")
		minimum = applicable_minimum_wage(state, region, min_wage_rates)
		state_ot = min(info["overtime_hours"], info["hours"])
		floor = minimum_wage_floor(info["hours"], state_ot, minimum)
		# Rounded to the cent BEFORE the comparison, because the cent is what gets
		# paid and stored. Comparing the unrounded share against the floor and then
		# rounding the sum can land a hundredth under it, which is not a wage
		# violation but would be reported as one by the independent check.
		earned_share = round(earned_gross * (info["hours"] / total_hours), 2)
		makeup = round(max(floor - earned_share, 0.0), 2) if applies else 0.0
		total_makeup += makeup
		total_floor += floor
		info["earned"] = earned_share
		info["makeup"] = makeup
		paid = round(earned_share + makeup, 2)
		minimum_wage_by_state[state] = {
			"state": state,
			"region": region,
			"hours": round(info["hours"], 2),
			"regular_hours": round(info["hours"] - state_ot, 2),
			"overtime_hours": round(state_ot, 2),
			"minimum_wage": minimum,
			"minimum_wage_floor": floor,
			"earned_wages": earned_share,
			"minimum_wage_makeup": makeup,
			"paid_wages": paid,
			# True by construction wherever the rule applies, because it was just
			# paid. On a Salary structure it is the honest verdict on a figure this
			# function deliberately did not raise — see above.
			"meets_minimum_wage": paid >= floor - 0.005,
			"effective_hourly_rate": (round(paid / info["hours"], 2) if info["hours"] > 0 else 0.0),
		}

	total_makeup = round(total_makeup, 2)
	gross_pay = round(earned_gross + total_makeup, 2)
	effective_rate = (
		round(gross_pay / total_hours, 2)
		if total_hours > 0
		else gross_result.get("effective_hourly_rate", 0.0)
	)

	# ── Minimum wage check ────────────────────────────────────────────
	#
	# Against the PAID gross, and it passes wherever a floor was known — which is
	# the point of the makeup above. Where no state was named on any shift there is
	# no floor to test, and this says so rather than inventing one.
	min_wage_result = check_minimum_wage(
		gross_pay,
		total_hours,
		primary_state,
		min_wage_rates=min_wage_rates,
		region=min_wage_regions.get(primary_state, "standard"),
		overtime_hours=overtime_hours,
	)
	meets_minimum_wage = (
		all(row["meets_minimum_wage"] for row in minimum_wage_by_state.values())
		if minimum_wage_by_state
		else min_wage_result["meets_minimum_wage"]
	)

	# ── Pre-tax deductions ────────────────────────────────────────────
	#
	# BEFORE THE ENGINES, because that is the entire difference between a
	# pre-tax deduction and a post-tax one: it is not withheld earlier, it is
	# withheld out of a smaller wage base. A 401(k) that came out after the
	# withholding calculation would be an after-tax contribution to a pre-tax
	# plan — the worker over-withheld all year and the plan out of compliance.
	#
	# TWO REDUCED BASES, NOT ONE. `federal_taxable_gross` is gross less every
	# pre-tax election; `fica_taxable_gross` is gross less only the ones that
	# also leave the FICA base. They differ by exactly the 401(k) deferral, and
	# collapsing them into one figure is the classic version of this bug —
	# see `payroll_deductions` for the statute on each side of it.
	deduction_rows = payroll_deductions.active_deductions(deductions or [])
	pre_tax = payroll_deductions.calculate_pre_tax_deductions(gross_pay, deduction_rows)
	federal_taxable_gross = pre_tax["federal_taxable_gross"]
	fica_taxable_gross = pre_tax["fica_taxable_gross"]

	# ── Federal taxes ─────────────────────────────────────────────────
	w4_data = tax_config.get("w4_data", {})
	fica_config = tax_config.get("fica_config", {})
	federal_tax_table = tax_config.get("federal_tax_table", [])
	ytd_gross = float(tax_config.get("ytd_gross", 0))
	ytd_ss_withheld = float(tax_config.get("ytd_ss_withheld", 0))

	federal = calculate_federal_withholding(
		federal_taxable_gross,
		pay_frequency,
		w4_data,
		ytd_gross,
		ytd_ss_withheld,
		fica_config,
		federal_tax_table,
		fica_gross=fica_taxable_gross,
	)

	# ── State taxes — per-state allocation for cross-state workers ────
	state_configs = tax_config.get("state_configs", {})
	state_tax_tables = tax_config.get("state_tax_tables", {})
	filing_status = w4_data.get("filing_status", "Single")

	state_results = {}
	total_state_employee = 0.0
	total_state_employer = 0.0
	total_state_suta = 0.0

	def _state_taxable(state: str, wages: float) -> float:
		"""One state's share of wages, less its share of the pre-tax elections.

		WHICH reduced base depends on what the state actually levies, and the two
		answers differ by the 401(k) — `payroll_deductions.STATE_TAXABLE_BASIS`
		carries the reasoning per state. Oregon taxes federal taxable income, so a
		deferral is out of its base; Washington levies no income tax and its
		employee-side premiums follow the FICA definition, so a deferral is in.

		PRORATED BY THE STATE'S SHARE OF GROSS on a cross-state slip, because a
		worker's 401(k) election is a property of the pay period rather than of
		one state's hours, and charging the whole deferral against whichever state
		happened to be computed first would move real money between two states'
		withholding.
		"""
		if gross_pay <= 0:
			return 0.0
		basis = payroll_deductions.STATE_TAXABLE_BASIS.get(state, "federal")
		removed = pre_tax["total"] if basis == "federal" else pre_tax["fica_exempt_total"]
		share = removed * (wages / gross_pay)
		return round(max(wages - share, 0.0), 2)

	if len(state_hours) <= 1:
		# Single-state: apply to full gross — makeup included, because a top-up to
		# the minimum wage is wages and is taxed as wages.
		if primary_state:
			state_hours[primary_state]["gross"] = gross_pay
		if primary_state and primary_state in state_configs:
			state_result = calculate_state_withholding(
				_state_taxable(primary_state, gross_pay),
				pay_frequency,
				primary_state,
				filing_status,
				state_configs[primary_state],
				state_tax_tables.get(primary_state),
				ytd_gross,
			)
			state_results[primary_state] = state_result
			ee_key = f"total_{primary_state.lower()}_employee"
			er_key = f"total_{primary_state.lower()}_employer"
			total_state_employee += state_result.get(ee_key, 0.0)
			total_state_employer += state_result.get(er_key, 0.0)
			total_state_suta += state_result.get("suta", 0.0)
	else:
		# Cross-state: allocate what was EARNED by hours proportion, then add each
		# state's own makeup to its own share. Allocating the topped-up total by
		# proportion instead would send Washington's makeup partly to Oregon.
		for ws, info in state_hours.items():
			if total_hours <= 0:
				continue
			# The same two figures the floor was tested against, so what a state is
			# taxed on and what it was checked against are the same number.
			state_gross = round(info.get("earned", 0.0) + info.get("makeup", 0.0), 2)
			info["gross"] = state_gross

			if ws in state_configs:
				state_result = calculate_state_withholding(
					_state_taxable(ws, state_gross),
					pay_frequency,
					ws,
					filing_status,
					state_configs[ws],
					state_tax_tables.get(ws),
					ytd_gross,
				)
				state_results[ws] = state_result
				ee_key = f"total_{ws.lower()}_employee"
				er_key = f"total_{ws.lower()}_employer"
				total_state_employee += state_result.get(ee_key, 0.0)
				total_state_employer += state_result.get(er_key, 0.0)
				total_state_suta += state_result.get("suta", 0.0)

	total_state_employee = round(total_state_employee, 2)
	total_state_employer = round(total_state_employer, 2)
	total_state_suta = round(total_state_suta, 2)

	# ── Aggregate deductions ──────────────────────────────────────────
	federal_withholding = federal["federal_income_tax"]
	social_security = federal["social_security_employee"]
	medicare = federal["medicare_employee"] + federal["additional_medicare"]

	statutory_deductions = round(
		federal_withholding + social_security + medicare + total_state_employee,
		2,
	)

	# ── Garnishments and post-tax deductions ──────────────────────────
	#
	# DISPOSABLE EARNINGS IS GROSS LESS THE STATUTORY WITHHOLDING AND NOTHING
	# ELSE. 29 CFR 870.10(a) says "amounts required by law to be withheld", and
	# the pre-tax elections above are not required by law however much they
	# reduced the tax — subtracting them here would let an employee shrink the
	# base a court order is measured against by raising their own 401(k) rate.
	disposable = payroll_deductions.disposable_earnings(gross_pay, statutory_deductions)
	garnishments = payroll_deductions.apply_garnishments(
		disposable,
		deduction_rows,
		pay_frequency=pay_frequency,
		state_cap_rate=tax_config.get("garnishment_state_cap_rate"),
	)

	# Post-tax voluntary deductions take what the orders left, and no more. A
	# garnishment outranks a union due, so the union due is what goes short.
	cash_left = round(
		max(gross_pay - pre_tax["total"] - statutory_deductions - garnishments["total"], 0.0), 2
	)
	post_tax = payroll_deductions.apply_post_tax_deductions(
		cash_left,
		deduction_rows,
		gross_pay,
		disposable,
	)

	deduction_lines = pre_tax["lines"] + garnishments["lines"] + post_tax["lines"]
	deduction_summary = payroll_deductions.summarize_deductions(deduction_lines)
	total_deduction_withholdings = deduction_summary["total"]

	# Inside `total_deductions`, so `net_pay = gross_pay - total_deductions`
	# stays the invariant every reader of a slip already relies on and net pay
	# stays what the worker is actually handed.
	total_deductions = round(statutory_deductions + total_deduction_withholdings, 2)
	net_pay = round(gross_pay - total_deductions, 2)

	# ── Employer taxes ────────────────────────────────────────────────
	#
	# v0.40.0. COMPUTED SINCE v0.28.0 AND REPORTED ONLY IN `federal_detail`
	# UNTIL NOW, which meant the slip written to the database carried the
	# employee's deductions and none of what the employer owed on top of them.
	# The GL posting needs them as figures rather than as a nested breakdown,
	# and so does anybody asking what a worker actually costs — the employer
	# share of FICA alone is 7.65% of gross that never appeared on a slip.
	#
	# Nothing here is a new charge and no total moves: these are the same
	# numbers `calculate_federal_withholding` and the state engines already
	# returned, lifted to the top level where they can be stored.
	social_security_employer = federal["social_security_employer"]
	medicare_employer = federal["medicare_employer"]
	futa = federal["futa_employer"]
	state_employer_other = round(total_state_employer - total_state_suta, 2)
	total_employer_taxes = round(
		social_security_employer + medicare_employer + futa + total_state_employer,
		2,
	)

	return {
		"employee": employee_data.get("employee", ""),
		"employee_name": employee_data.get("employee_name", ""),
		"salary_structure": salary_structure.get("name", ""),
		"pay_type": pay_type,
		"work_state": primary_state,
		"total_hours": round(total_hours, 2),
		"regular_hours": round(regular_hours, 2),
		"overtime_hours": round(overtime_hours, 2),
		"piece_units": round(piece_units, 2),
		"piece_rate": base_rate if pay_type == "Piece Rate" else 0,
		"gross_pay": gross_pay,
		# v0.49.0. The three figures the higher-of rule is made of, kept apart so a
		# payroll report can say WHY somebody was paid what they were paid: what the
		# work earned, what the hours were owed, and the difference that was added.
		"earned_gross": round(earned_gross, 2),
		"minimum_wage_floor": round(total_floor, 2),
		"minimum_wage_makeup": total_makeup,
		"minimum_wage_by_state": minimum_wage_by_state,
		"federal_withholding": federal_withholding,
		"state_withholding": total_state_employee,
		"social_security": social_security,
		"medicare": medicare,
		"state_taxes_detail": state_results,
		# The taxes on their own, kept apart from the total now that the total
		# carries other things. Every release before this one had these two as
		# the same number, and a report that assumed so should read this one.
		"statutory_deductions": statutory_deductions,
		# ── Garnishments and voluntary deductions ────────────────────────
		#
		# `deduction_lines` is the itemised list a pay stub prints line by line;
		# the totals beside it are what a register column and a GL posting want.
		# The two taxable bases are stored rather than derived because a W-2 is
		# built from them: Box 1 is the federal one, Box 3 the FICA one, and on a
		# slip carrying a 401(k) they differ by exactly the deferral.
		"deduction_lines": deduction_lines,
		"total_deduction_withholdings": total_deduction_withholdings,
		"pre_tax_deductions": deduction_summary["pre_tax_total"],
		"post_tax_deductions": deduction_summary["post_tax_total"],
		"garnishment_total": deduction_summary["garnishment_total"],
		"voluntary_deduction_total": deduction_summary["voluntary_total"],
		"deductions_by_category": deduction_summary["by_category"],
		"federal_taxable_gross": federal_taxable_gross,
		"fica_taxable_gross": fica_taxable_gross,
		"disposable_earnings": disposable,
		"garnishment_detail": garnishments,
		# What an order asked for and the pay could not give. NOT carried
		# forward into the next period — see `payroll_deductions` — so this list
		# is the only place it is said, and somebody has to read it.
		"deduction_shortfalls": (pre_tax["shortfalls"] + garnishments["shortfalls"] + post_tax["shortfalls"]),
		"total_deductions": total_deductions,
		"net_pay": net_pay,
		"social_security_employer": social_security_employer,
		"medicare_employer": medicare_employer,
		"futa": futa,
		"state_unemployment": total_state_suta,
		"state_employer_other": state_employer_other,
		"state_employer_taxes": total_state_employer,
		"total_employer_taxes": total_employer_taxes,
		"total_cost_of_employment": round(gross_pay + total_employer_taxes, 2),
		"minimum_wage_check": meets_minimum_wage,
		# The old flat verdict — the whole period's gross against the floor of the
		# state holding the most hours — kept because it is what releases before
		# v0.49.0 stored and because a disagreement between it and the per-state
		# answer is itself worth seeing. `minimum_wage_check` is the per-state one.
		"minimum_wage_flat_check": min_wage_result["meets_minimum_wage"],
		"minimum_wage_detail_flat": min_wage_result,
		"effective_hourly_rate": effective_rate,
		"gross_detail": gross_result,
		"federal_detail": federal,
		"state_hours": state_hours,
	}

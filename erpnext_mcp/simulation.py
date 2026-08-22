# SPDX-License-Identifier: MIT
"""Weather impact: what a run of temperatures did to a block, from the numbers alone.

WHAT THIS ANSWERS. Given the hourly temperatures a block actually saw — from
`Farm Shift Weather Reading`, from an on-site station, or from the archive
backfill — how much heat did the crop accumulate, how much winter chill did it
bank, how many hours was it below freezing during bloom, and how many hours was
it hot enough to matter. Four questions, four models, all of them arithmetic
over a series, none of them needing a network call or a database.

WHY IT IS PURE. The farm_app's version reached into its weather module for the
series, its ORM for the crop's thresholds, and numpy for a mean, which meant the
model could not be tested without all three and in practice never was — its
`integrate_pest_boost` called a `simulate_dynamics` that does not exist in the
file, and nothing caught it because nothing could run it. Here the series comes
in as an argument and the answer goes out as a dict; the callers that fetch and
store are somewhere else and are the part allowed to be hard to test.

THE FOUR MODELS, AND WHAT EACH ONE IS FOR.

*Growing degree days* drive every pest emergence model in `ipm_reference.py` and
every harvest-window estimate. Computed by the AVERAGING method with an upper
cutoff — `((min+max)/2 - base)`, clamped at zero — because that is the method
the degree-day tables in that module were built against. Using the single-sine
method here instead would shift accumulations by 5-10% against the biofix dates
those tables publish, which is the difference between spraying for codling moth
on the right day and the wrong one.

*Chill accumulation* is the tree-fruit question the farm_app never asked and this
farm needs: a cherry that does not bank enough winter chill breaks dormancy
raggedly and sets a short crop, and by the time that is visible in April the
season is decided. Two models, because the two disagree and orchardists use
both: `chill_hours` counts hours between 0 and 7.2°C (the Weinberger model, the
one every extension bulletin quotes), and `chill_units` is the Utah model, which
gives partial credit near the edges of that band and DEBITS warm winter hours —
which is why a warm January can move the two figures in opposite directions.

*Frost exposure* is counted in HOURS BELOW A THRESHOLD and reported alongside the
coldest reading, because bloom damage is a function of both duration and depth
and neither alone predicts it. The threshold belongs to the caller: the critical
temperature for a cherry at first white is not the one for the same block at
green tip, and a module that hard-coded 0°C would report no frost on the night
that cost the crop.

*Heat exposure* is the same shape at the other end, and matters for two separate
reasons this reports separately: fruit at risk of sunburn, and a crew at risk
under the state heat rule.

WHAT IS NOT HERE. There is no yield-impact percentage. The farm_app had one —
`- (frost_days * 5 + heat_days * 3)` — with no citation and no calibration, and
a number like that printed next to real measurements gets quoted in an insurance
claim. Everything in this module is a count or an accumulation that a person can
check against their own thermometer. Turning those into a yield forecast is a
judgement, and it is not one arithmetic can make on its own.
"""

from __future__ import annotations

#: The chill band in Celsius, per the Weinberger model. Hours strictly inside it
#: count; the endpoints are the published bounds and are included.
CHILL_LOW_C = 0.0
CHILL_HIGH_C = 7.2

#: The Utah model's weighting, `(upper bound °C, credit)`, evaluated in order —
#: the first band an hour falls at or below is the one it scores. The negative
#: credits above 16°C are the model's whole point: a warm spell in January
#: removes chill the orchard had already banked.
UTAH_BANDS = (
	(1.4, 0.0),
	(2.4, 0.5),
	(9.1, 1.0),
	(12.4, 0.5),
	(15.9, 0.0),
	(18.0, -0.5),
	(float("inf"), -1.0),
)

#: The default base temperature for growing degree days, in Celsius. 10°C is the
#: generic plant base; every pest model in `ipm_reference.py` states its own and
#: callers should pass it rather than accept this.
DEFAULT_GDD_BASE_C = 10.0

#: The default upper cutoff. Above it, additional heat does not speed
#: development further, and counting it inflates an accumulation against the
#: tables it will be compared with.
DEFAULT_GDD_CUTOFF_C = 30.0

#: Sunburn risk on fruit begins around a 43°C fruit surface temperature, which
#: on a clear day corresponds to roughly this air temperature. A default, not a
#: finding — orientation, canopy and wind all move it.
SUNBURN_AIR_C = 35.0

#: Most hours one call will process. A year of hourly readings is 8,760; a series
#: longer than this is a caller that meant to filter by date and did not, and
#: summing it silently would answer a question nobody asked.
MAX_HOURS = 20000


class SimulationError(ValueError):
	"""A series that cannot be summarised — too long, or holding no readings."""


def to_celsius(value, unit: str = "C"):
	"""A temperature in Celsius from either scale, or `None` if it is not a number.

	The unit is explicit because this app stores Fahrenheit on
	`Farm Shift Weather Reading.temperature_f` and Celsius almost everywhere
	else, and a series silently read in the wrong one produces a frost count of
	zero on a night that killed the crop.
	"""
	if value is None or isinstance(value, bool):
		return None
	try:
		number = float(value)
	except (TypeError, ValueError):
		return None
	if number != number:  # NaN
		return None
	scale = str(unit or "C").strip().upper().lstrip("°")
	if scale in ("C", "CELSIUS"):
		return number
	if scale in ("F", "FAHRENHEIT"):
		return (number - 32.0) * 5.0 / 9.0
	raise SimulationError(f"temperature unit {unit!r} is not one of C or F")


def readings(series, unit: str = "C") -> list:
	"""Every usable temperature in a series, in Celsius, blanks dropped.

	Accepts a list of numbers, or a list of dicts carrying a `temperature`,
	`temperature_c`, `temperature_f` or `value` key — which is what
	`frappe.db.get_all` hands back. A dict naming `temperature_f` is read as
	Fahrenheit whatever `unit` says, because a column whose name states its unit
	outranks an argument's default.
	"""
	out = []
	for item in series or []:
		if isinstance(item, dict):
			if item.get("temperature_f") is not None:
				value, item_unit = item["temperature_f"], "F"
			elif item.get("temperature_c") is not None:
				value, item_unit = item["temperature_c"], "C"
			else:
				value, item_unit = item.get("temperature", item.get("value")), unit
		else:
			value, item_unit = item, unit
		celsius = to_celsius(value, item_unit)
		if celsius is not None:
			out.append(celsius)
		if len(out) > MAX_HOURS:
			raise SimulationError(
				f"more than {MAX_HOURS} readings in one series — that is over two years of hourly "
				"data. Filter by date range before summarising."
			)
	return out


def growing_degree_days(daily_min_c, daily_max_c, base_c=DEFAULT_GDD_BASE_C, cutoff_c=DEFAULT_GDD_CUTOFF_C):
	"""One day's degree days by the averaging method, never negative.

	Both the minimum and the maximum are clamped to the cutoff BEFORE the mean is
	taken, which is the standard upper-cutoff treatment and is not the same as
	clamping the result: a day running 18-38°C with a 30°C cutoff contributes
	`(18+30)/2 - 10 = 14`, not `(18+38)/2 - 10 = 18`.
	"""
	low = to_celsius(daily_min_c)
	high = to_celsius(daily_max_c)
	if low is None or high is None:
		return None
	if low > high:
		low, high = high, low
	ceiling = to_celsius(cutoff_c)
	if ceiling is not None:
		low, high = min(low, ceiling), min(high, ceiling)
	floor = to_celsius(base_c) or 0.0
	return max(0.0, (low + high) / 2.0 - floor)


def accumulate_gdd(days, base_c=DEFAULT_GDD_BASE_C, cutoff_c=DEFAULT_GDD_CUTOFF_C) -> dict:
	"""`{total, days, daily}` over a list of `(min, max)` pairs or `{min,max}` dicts.

	`daily` is kept so a caller can find the day a biofix threshold was crossed,
	which is the question a degree-day model is actually asked — "how many" is
	rarely useful without "and when".
	"""
	daily, skipped = [], 0
	for entry in days or []:
		if isinstance(entry, dict):
			low = entry.get("min") if entry.get("min") is not None else entry.get("min_temp_c")
			high = entry.get("max") if entry.get("max") is not None else entry.get("max_temp_c")
			label = entry.get("date")
		else:
			try:
				low, high = entry
			except (TypeError, ValueError):
				skipped += 1
				continue
			label = None
		value = growing_degree_days(low, high, base_c, cutoff_c)
		if value is None:
			skipped += 1
			continue
		daily.append({"date": label, "gdd": round(value, 3)})
	return {
		"total": round(sum(item["gdd"] for item in daily), 3),
		"days": len(daily),
		"skipped": skipped,
		"base_c": to_celsius(base_c),
		"cutoff_c": to_celsius(cutoff_c),
		"daily": daily,
	}


def chill_hours(series, unit: str = "C") -> float:
	"""Hours in the 0-7.2°C band. The Weinberger model, one hour per reading.

	Assumes one reading per hour, which is what the archive and the station both
	produce. A series at another interval is a caller's unit problem and this
	does not guess at it — a fifteen-minute series would over-count fourfold and
	the fix is to resample before calling, not to add a parameter nobody sets.
	"""
	return float(sum(1 for value in readings(series, unit) if CHILL_LOW_C <= value <= CHILL_HIGH_C))


def chill_units(series, unit: str = "C") -> float:
	"""Utah-model chill units, which can be negative after a warm winter."""
	total = 0.0
	for value in readings(series, unit):
		for ceiling, credit in UTAH_BANDS:
			if value <= ceiling:
				total += credit
				break
	return round(total, 2)


def exposure(series, threshold_c, direction: str = "below", unit: str = "C") -> dict:
	"""`{hours, extreme, mean_excess, readings}` against a threshold.

	One function for frost and for heat because they are the same computation
	pointed two ways, and two near-identical ones would drift. `extreme` is the
	coldest (or hottest) reading in the series and `mean_excess` is the average
	distance past the threshold across the hours that crossed it — depth as well
	as duration, for the reason in the module docstring.

	`unit` DESCRIBES THE SERIES AND NEVER THE THRESHOLD. The threshold is named
	`threshold_c` and is always Celsius. An earlier draft converted it with the
	series, so `heat(fahrenheit_series, threshold_c=35, unit="F")` compared the
	readings against 1.7°C and reported every hour of the summer as heat stress —
	a caller reading its own argument names would never have guessed it, which is
	why the two are separated here in as many words.
	"""
	way = str(direction or "below").strip().lower()
	if way not in ("below", "above"):
		raise SimulationError(f"direction must be 'below' or 'above'; got {direction!r}")
	limit = to_celsius(threshold_c, "C")
	if limit is None:
		raise SimulationError(f"threshold {threshold_c!r} is not a temperature")

	values = readings(series, unit)
	if not values:
		return {"hours": 0, "extreme": None, "mean_excess": 0.0, "readings": 0, "threshold_c": limit}
	crossed = [value for value in values if (value < limit if way == "below" else value > limit)]
	excess = [abs(value - limit) for value in crossed]
	return {
		"hours": len(crossed),
		"extreme": round(min(values) if way == "below" else max(values), 2),
		"mean_excess": round(sum(excess) / len(excess), 2) if excess else 0.0,
		"readings": len(values),
		"threshold_c": limit,
	}


def frost(series, threshold_c=0.0, unit: str = "C") -> dict:
	"""Frost exposure. The threshold is the caller's, and it should be the crop's.

	See the module docstring: the critical temperature at first white is not the
	one at green tip, and 0°C is a default rather than an answer.
	"""
	return exposure(series, threshold_c, "below", unit)


def heat(series, threshold_c=SUNBURN_AIR_C, unit: str = "C") -> dict:
	"""Heat exposure, defaulting to the air temperature sunburn risk begins at."""
	return exposure(series, threshold_c, "above", unit)


def summarise(series, unit: str = "C", gdd_base_c=DEFAULT_GDD_BASE_C, frost_c=0.0, heat_c=SUNBURN_AIR_C):
	"""Everything above over one hourly series, as one dict.

	The degree-day figure here is derived from the series' OWN daily minima and
	maxima, grouped in 24-hour blocks in the order given. That makes it right for
	a series that is genuinely hourly and in order, and wrong for one that is
	not — so it is reported as `gdd_estimated` with the block count beside it,
	and a caller with real daily extremes should use `accumulate_gdd` instead.
	"""
	values = readings(series, unit)
	if not values:
		raise SimulationError("no usable temperature readings in the series")

	blocks = [values[index : index + 24] for index in range(0, len(values), 24)]
	estimated = accumulate_gdd([(min(block), max(block)) for block in blocks], gdd_base_c)
	return {
		"readings": len(values),
		"mean_c": round(sum(values) / len(values), 2),
		"min_c": round(min(values), 2),
		"max_c": round(max(values), 2),
		"chill_hours": chill_hours(values),
		"chill_units": chill_units(values),
		"frost": frost(values, frost_c),
		"heat": heat(values, heat_c),
		"gdd_estimated": estimated["total"],
		"gdd_blocks": len(blocks),
		"gdd_base_c": to_celsius(gdd_base_c),
	}

# SPDX-License-Identifier: MIT
"""CCF context providers for work timing: weather and phenology. v0.239.0.

docs/design/ccf_core_work_timing.md §2.2, §4, §5; Tim's decisions 3, 12, 15, 19, 22 (2026-10-04).
Registered into `ccf.PROVIDERS` on import (`ccf` imports this at the end).

WEATHER — Open-Meteo through the existing `services.weather` plumbing (Weather Settings URLs,
timeouts, back-off), per BLOCK: the block's centroid, cached per H3 resolution-7 cell (decision
22), so forty tasks in one block are one request. A 16-day daily forecast, 48 hours hourly, and the
last three days (`past_days`) for "hours since rain". RAIN RISK IS PROBABILISTIC (decision 15):
`forecast.daily[].rain_risk_cum_pct` is the chance of at least one wet day from today through that
day, from the daily probabilities — so a rule says "Hold if the chance of rain within 7 days is 40%
or more", and when the forecast changes the next evaluation clears the Hold by itself. Rain is "wet"
above `WET_DAY_IN` (0.05 in, decision 19; a rule can use its own number on the daily amount).

v0.253.0 — PROBABILITY AND AMOUNT TOGETHER, AND THE RULE'S OWN THRESHOLD. Open-Meteo's daily
probability is the chance of any measurable rain (0.1 mm), which says nothing about whether a day
passes 0.05 in. Each day now also carries `rain` = {p, in} and the chance of rain OVER a threshold:
P(over T) = p × exp(−(T − trace) / μ), μ = the forecast amount ÷ p (the mean amount on a wet day,
never under 0.02 in) — the exponential wet-day amount of the standard stochastic weather generators
(Richardson 1981). `p_over_pct` / `wet_risk_cum_pct` use 0.05 in; a rule reads its own with the
`chance_over` aggregation and an `over` in inches:
  {"path": "weather.forecast.daily[0..6].rain", "agg": "chance_over", "over": 0.05, "op": "lt", "value": 40}
— "the chance of at least one day over 0.05 in this week is under 40%". `rain_risk_cum_pct` is unchanged.

Off until `weather_forecast_enabled` (Weather Settings is Desk-only, design §4): with it off the
provider answers nothing and every weather check is "no data" — a Hold that says why.

PHENOLOGY — the block's latest observed BBCH stage (Crop Observation `growth_stage_code`), when and
how old. Decision 12's "estimate from calendar + degree days" arrives with degree days (queue item 10).
"""

from __future__ import annotations

import datetime
import math

import frappe

from . import compat

WET_DAY_IN = 0.05
#: v0.253.0. Open-Meteo's precipitation probability is for more than 0.1 mm.
TRACE_IN = 0.004
#: v0.253.0. The smallest mean wet-day amount the model assumes when a day has a chance of rain but
#: the forecast amount is (near) nothing — drizzle, not zero. Local judgment.
WET_MEAN_FLOOR_IN = 0.02
#: v0.253.0. The largest `over` a rule may give (inches in a day).
OVER_MAX_IN = 5.0
FORECAST_DAYS = 16
HOURLY_HOURS = 48
PAST_DAYS = 3
H3_RESOLUTION = 7
#: How long one cell's forecast is reused.
TTL_SECONDS = 2 * 60 * 60

_CELL_CACHE: dict = {}


def forecast_enabled() -> bool:
	try:
		from .services import weather

		return bool(compat.checked(weather._setting("forecast_enabled")))
	except Exception:
		return False


def block_point(subject: dict, ctx: dict) -> tuple | None:
	"""(lat, lon, field) for the subject's block: a Farm Task's Field location, or the row's own `block`."""
	field = None
	if str(subject.get("location_doctype") or "") == "Field" and subject.get("location"):
		field = subject["location"]
	elif subject.get("block") and str(subject.get("block_doctype") or "Field") == "Field":
		field = subject["block"]
	elif ctx.get("doctype") == "Field":
		field = subject.get("name")
	if not field or not compat.doctype_exists("Field"):
		return None
	row = frappe.db.get_value(
		"Field", field, compat.existing_fields("Field", ("boundary_centroid_lat", "boundary_centroid_lon")), as_dict=True
	) or {}
	try:
		return float(row["boundary_centroid_lat"]), float(row["boundary_centroid_lon"]), field
	except (KeyError, TypeError, ValueError):
		return None


def _cell(lat: float, lon: float) -> str:
	try:
		import h3

		return (h3.latlng_to_cell if hasattr(h3, "latlng_to_cell") else h3.geo_to_h3)(lat, lon, H3_RESOLUTION)
	except Exception:
		return f"{round(lat, 2)},{round(lon, 2)}"


def fetch_forecast(lat: float, lon: float) -> dict | None:
	"""The cell's normalised forecast, from cache or one request. None on any failure."""
	from .services import weather

	cell = _cell(lat, lon)
	now = weather._clock()
	cached = _CELL_CACHE.get(cell)
	if cached and cached[0] > now:
		return cached[1]
	payload = weather._get_json(
		weather.base_url("current"),
		{
			"latitude": lat,
			"longitude": lon,
			"daily": "precipitation_sum,precipitation_probability_max,temperature_2m_min,temperature_2m_max,"
			"wind_speed_10m_max,wind_gusts_10m_max",
			"hourly": "temperature_2m,precipitation,precipitation_probability,wind_speed_10m",
			"forecast_days": FORECAST_DAYS,
			"past_days": PAST_DAYS,
			"timezone": "auto",
			"precipitation_unit": "inch",
			**weather.UNITS,
		},
		scope=f"forecast:{cell}",
	)
	if not isinstance(payload, dict):
		return None
	normalised = normalise(payload, cell)
	_CELL_CACHE[cell] = (now + TTL_SECONDS, normalised)
	return normalised


def _num(value):
	try:
		return None if value is None else float(value)
	except (TypeError, ValueError):
		return None


def p_over(prob_pct, amount_in, over_in: float = WET_DAY_IN) -> float:
	"""v0.253.0. The chance (0–1) that one day's rain passes `over_in`, from the chance of any rain and
	the forecast amount. Pure."""
	p = min(max((_num(prob_pct) or 0.0) / 100.0, 0.0), 1.0)
	amount = _num(amount_in)
	if p == 0:
		if amount is None or amount < 0.01:
			return 0.0
		p = 1.0  # the model rains while the ensemble says nothing — believe the amount
	mean_wet = max((amount or 0.0) / p, WET_MEAN_FLOOR_IN)
	return p * math.exp(-max(float(over_in) - TRACE_IN, 0.0) / mean_wet)


def chance_any(chances) -> float:
	"""v0.253.0. The chance (0–1) that at least one of independent events happens."""
	dry = 1.0
	for chance in chances:
		dry *= 1 - min(max(float(chance), 0.0), 1.0)
	return 1 - dry


def _cumulate(ahead: list) -> None:
	"""The two running risks over the days ahead, in place: any rain (decision 15, as since v0.239.0)
	and rain over 0.05 in (v0.253.0)."""
	dry_so_far = 1.0
	running = []
	for row in ahead:
		p = (row.get("precip_prob_pct") or 0) / 100.0
		if row.get("precip_in") is not None and row["precip_in"] > WET_DAY_IN and p == 0:
			p = 1.0
		dry_so_far *= 1 - min(max(p, 0.0), 1.0)
		row["rain_risk_cum_pct"] = round((1 - dry_so_far) * 100, 1)
		running.append(p_over(row.get("precip_prob_pct"), row.get("precip_in")))
		row["wet_risk_cum_pct"] = round(chance_any(running) * 100, 1)


def normalise(payload: dict, cell: str = "", today: str = "") -> dict:
	"""Open-Meteo's answer as the provider's paths. Pure: tested without a network."""
	daily = payload.get("daily") or {}
	hourly = payload.get("hourly") or {}
	today = today or str(frappe.utils.today())[:10]
	days = list(daily.get("time") or [])
	rows = []
	for index, day in enumerate(days):
		rows.append(
			{
				"date": day,
				"precip_in": _num((daily.get("precipitation_sum") or [None] * len(days))[index]),
				"precip_prob_pct": _num((daily.get("precipitation_probability_max") or [None] * len(days))[index]),
				"tmin_f": _num((daily.get("temperature_2m_min") or [None] * len(days))[index]),
				"tmax_f": _num((daily.get("temperature_2m_max") or [None] * len(days))[index]),
				"wind_mph": _num((daily.get("wind_speed_10m_max") or [None] * len(days))[index]),
				"gust_mph": _num((daily.get("wind_gusts_10m_max") or [None] * len(days))[index]),
			}
		)
		row = rows[-1]
		row["rain"] = {"p": row["precip_prob_pct"], "in": row["precip_in"]}
		row["p_over_pct"] = round(p_over(row["precip_prob_pct"], row["precip_in"]) * 100, 1)
	past = [r for r in rows if r["date"] < today]
	ahead = [r for r in rows if r["date"] >= today]
	# Decision 15: the chance of at least one wet day from today through day i.
	_cumulate(ahead)
	times = list(hourly.get("time") or [])
	hours = []
	for index, when in enumerate(times):
		hours.append(
			{
				"time": when,
				"temp_f": _num((hourly.get("temperature_2m") or [None] * len(times))[index]),
				"precip_in": _num((hourly.get("precipitation") or [None] * len(times))[index]),
				"precip_prob_pct": _num((hourly.get("precipitation_probability") or [None] * len(times))[index]),
				"wind_mph": _num((hourly.get("wind_speed_10m") or [None] * len(times))[index]),
			}
		)
	now_hour = datetime.datetime.now().strftime("%Y-%m-%dT%H:00")
	ahead_hours = [h for h in hours if h["time"] >= now_hour][:HOURLY_HOURS]
	last_wet = max((h["time"] for h in hours if h["time"] < now_hour and (h["precip_in"] or 0) >= 0.01), default=None)
	hours_since = None
	if last_wet:
		try:
			delta = datetime.datetime.fromisoformat(now_hour) - datetime.datetime.fromisoformat(last_wet)
			hours_since = round(delta.total_seconds() / 3600, 1)
		except ValueError:
			hours_since = None
	elif hours:
		hours_since = float(PAST_DAYS * 24)  # dry for the whole window we can see
	dry_streak = 0
	for row in reversed(past):
		if (row["precip_in"] or 0) > WET_DAY_IN:
			break
		dry_streak += 1
	return {
		"forecast": {"daily": ahead, "hourly": ahead_hours},
		"recent": {"hours_since_rain": hours_since, "last_rain_end": last_wet, "dry_streak_days": dry_streak},
		"meta": {"source": "Open-Meteo forecast API", "fetched_at": frappe.utils.now(), "h3": cell},
	}


def as_of(values: dict, day) -> dict:
	"""The forecast as seen from a later day (v0.242.0): days before it drop off and the rain risk
	restarts there. Judging "tomorrow" against a forecast whose day 0 is today would repeat today."""
	day = str(day or "")[:10]
	daily = ((values or {}).get("forecast") or {}).get("daily") or []
	if not daily or not day or day <= str(daily[0].get("date") or ""):
		return values
	ahead = [dict(row) for row in daily if str(row.get("date") or "") >= day]
	_cumulate(ahead)
	return {**values, "forecast": {**values["forecast"], "daily": ahead}}


def _weather(subject: dict, ctx: dict) -> dict:
	if not forecast_enabled():
		return {}
	point = block_point(subject, ctx)
	if not point:
		return {}
	return fetch_forecast(point[0], point[1]) or {}


def _phenology(subject: dict, ctx: dict) -> dict:
	point_field = None
	if str(subject.get("location_doctype") or "") == "Field":
		point_field = subject.get("location")
	elif subject.get("block"):
		point_field = subject.get("block")
	if not point_field or not compat.doctype_exists("Crop Observation"):
		return {}
	rows = frappe.db.get_all(
		"Crop Observation",
		filters={"block": point_field, "growth_stage_code": ("not in", ["", None])},
		fields=["growth_stage_code", "observed_at", "observed_on", "crop_stage"],
		order_by="observed_at desc",
		limit=1,
	)
	if not rows:
		return {}
	row = rows[0]
	seen = str(row.get("observed_at") or row.get("observed_on") or "")[:10]
	age = None
	try:
		age = (ctx["as_of_date"] - datetime.date.fromisoformat(seen)).days
	except (KeyError, ValueError):
		pass
	return {"bbch": str(row.get("growth_stage_code")), "bbch_observed_at": seen or None, "bbch_age_days": age,
	        "stage_name": row.get("crop_stage")}


def _chance_over(series: list, leaf: dict):
	"""`chance_over`: % chance that at least one day of the window passes the leaf's `over` inches."""
	over = float(leaf.get("over", WET_DAY_IN))
	days = [d for d in series if isinstance(d, dict)]
	if not days:
		return None
	return round(chance_any(p_over(d.get("p"), d.get("in"), over) for d in days) * 100, 1)


def _check_over(leaf: dict, where: str) -> None:
	if "over" not in leaf:
		return
	over = leaf["over"]
	if not isinstance(over, (int, float)) or isinstance(over, bool) or not TRACE_IN < over <= OVER_MAX_IN:
		from . import ccf

		raise ccf.TreeError(f"{where}: over is a daily rain amount in inches, more than {TRACE_IN} and at most {OVER_MAX_IN:g}.")


def register(ccf) -> None:
	p = ccf._p
	ccf.register_aggregation(
		ccf.Aggregation(
			"chance_over",
			_chance_over,
			_check_over,
			("rain",),
			"% chance that at least one day in the window rains more than `over` inches (default 0.05, decision 19): "
			"each day's chance of any rain and forecast amount, an exponential wet-day amount, days independent.",
			{"over": "inches in a day, default 0.05"},
		)
	)
	ccf.register(
		ccf.Provider(
			"weather",
			{
				"forecast.daily[].precip_in": p("number", "Daily rain.", "in", 0.12),
				"forecast.daily[].precip_prob_pct": p("number", "Daily chance of rain.", "%", 40),
				"forecast.daily[].rain_risk_cum_pct": p("number", "Chance of at least one wet day from today through this day (decision 15).", "%", 55),
				"forecast.daily[].rain": p("rain", "The day's chance of rain and forecast amount, for agg chance_over (v0.253.0).", example={"p": 40, "in": 0.12}),
				"forecast.daily[].p_over_pct": p("number", "Chance this day rains more than 0.05 in (v0.253.0).", "%", 25),
				"forecast.daily[].wet_risk_cum_pct": p("number", "Chance of a day over 0.05 in from today through this day (v0.253.0).", "%", 35),
				"forecast.daily[].tmin_f": p("number", "Daily low.", "°F", 27),
				"forecast.daily[].tmax_f": p("number", "Daily high.", "°F", 48),
				"forecast.daily[].wind_mph": p("number", "Daily max wind.", "mph", 8),
				"forecast.daily[].gust_mph": p("number", "Daily max gust.", "mph", 15),
				"forecast.hourly[].temp_f": p("number", "Hourly temperature, next 48 h.", "°F", 31),
				"forecast.hourly[].precip_in": p("number", "Hourly rain, next 48 h.", "in", 0.0),
				"forecast.hourly[].precip_prob_pct": p("number", "Hourly chance of rain.", "%", 20),
				"forecast.hourly[].wind_mph": p("number", "Hourly wind.", "mph", 6),
				"recent.hours_since_rain": p("number", "Hours since the last measurable rain (past 3 days).", "h", 60),
				"recent.dry_streak_days": p("number", "Dry days in a row before today.", "days", 2),
				"recent.last_rain_end": p("string", "When it last rained.", example="2026-01-14T06:00"),
				"meta.source": p("string", "Where the forecast came from.", example="Open-Meteo forecast API"),
				"meta.fetched_at": p("string", "When it was fetched.", example="2026-01-15 05:40:00"),
				"meta.h3": p("string", "The H3 cell (resolution 7) it was fetched for.", example="872830828ffffff"),
			},
			lambda subject, ctx: as_of(_weather(subject, ctx), ctx.get("as_of_date")),
			past=False,
			description="Open-Meteo per block (H3 resolution 7), through Weather Settings. Off until forecast_enabled.",
		)
	)
	ccf.register(
		ccf.Provider(
			"phenology",
			{
				"bbch": p("bbch", "The block's latest observed BBCH stage code.", example="51"),
				"bbch_observed_at": p("date", "When it was observed.", example="2026-03-02"),
				"bbch_age_days": p("number", "Days since that observation.", "days", 6),
				"stage_name": p("string", "The stage's name as recorded.", example="Bud swell"),
			},
			lambda subject, ctx: _phenology(subject, ctx),
			past=True,
			description="The block's latest Crop Observation with a growth stage.",
		)
	)

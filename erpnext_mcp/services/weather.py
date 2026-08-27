# SPDX-License-Identifier: MIT
"""Open-Meteo, the fifteen-minute sweep, and the events a reading is allowed to write.

v0.19.4. THE TABLE v0.19.3 SHIPPED EMPTY IS THE POINT OF THIS MODULE. A Farm
Shift has carried a `weather_timeline` since the shift became the anchor, and
nothing wrote to it: the shape was fixed first so that wiring a fetch would not
mean migrating a schema under live compliance records. This is the fetch.

Ported in SHAPE rather than in code from `farm_app/app/utils/weather.py`, which
is the Flask side's agronomy module — soil temperature at four depths, chill
hours, growing degree days, evapotranspiration, field capacity. Not one of those
functions is what a shift needs, and none of them is reused here: what carried
over is the idiom. One `requests.get` with an explicit timeout, `raise_for_status`,
a normalised dict out, an error path that returns rather than throws, and
Open-Meteo's two hosts as the source of truth because it is free, needs no API
key, and answers for any coordinate on earth.

────────────────────────────────────────────────────────────────────────────
WHAT THIS MODULE MAY AND MAY NOT DECIDE
────────────────────────────────────────────────────────────────────────────

IT LOGS OBSERVATIONS. IT DOES NOT FILE COMPLIANCE RECORDS. A weather reading is
a fact about a place and a moment, and so is "this reading is above the threshold
in Weather Settings" — both are arithmetic, both are written automatically, and a
Threshold Crossed compliance event is exactly that and nothing more.

IT RINGS ONE PHONE ON A HEAT CROSSING, AND THAT IS NOT A COMPLIANCE DECISION
EITHER. v0.140.0. The timeline has said "the heat index reached 84 °F at 11:45"
since v0.19.4, and until now the only person who could act on that — the foreman
standing on the block — found out by opening the app. A notification is not a
record and does not claim to be one: it carries the shift, the place and the two
numbers, and what it asks for is that somebody LOOK. Once per shift, addressed to
the crew leader by name, and never on a backfill — see `_push_heat_crossing` and
`heat_announced_for` for the whole of it.

A HEAT EXPOSURE EVENT IS NOT WRITTEN HERE AND WILL NOT BE. That record says which
crew was exposed, whether water was provided at the required rate, whether the
rest cycle was actually taken rather than merely offered, whether anybody showed
signs and what was done about it — five judgements about people, made by the
foreman who was there, attested with their signature. A machine that filed one
from a temperature reading would be producing a document with nobody behind it,
in the exact place where having nobody behind it is the failure. The sweep
surfaces the condition; the foreman decides whether it is a record.

────────────────────────────────────────────────────────────────────────────
THE HEAT INDEX IS COMPUTED HERE AND NOT TAKEN FROM THE API
────────────────────────────────────────────────────────────────────────────

Open-Meteo returns `apparent_temperature`, which is not the same number.
Apparent temperature folds in wind and solar radiation and is a wind-chill figure
in winter; the NWS heat index is temperature and relative humidity, and it is
what OAR 437-004-1131 turns on. At 88 °F and 70 % humidity the two disagree by
about ten degrees, and the direction of the disagreement is the one that matters:
the heat index says 100 °F and the rule engages, and a shift documented against
the other number would look compliant while somebody was being cooked.

So `heat_index_f` is the Rothfusz regression the NWS publishes, computed from the
temperature and humidity the API DID return, both of which are stored beside it so
a disputed index can be recomputed from the observation. `apparent_temperature` is
requested anyway and kept in the raw response: it is the fallback when a reading
comes back with no humidity, and it is worth having when somebody asks why the
crew was struggling at a heat index the rule says is fine.

────────────────────────────────────────────────────────────────────────────
EVERY FAILURE IS A MISSING READING AND NOTHING WORSE
────────────────────────────────────────────────────────────────────────────

`sweep_open_shifts` runs on a cron on somebody's bench with nobody watching,
beside their real work. Open-Meteo is a free service with no authentication,
which is a reason to be MORE careful with it and not less — the only thing
between this app and a rate limit is the cache and the backoff below. So:

  * A 429 or a 5xx starts an exponential backoff for that coordinate, doubling
    each time and capped at an hour, and every subsequent call for that place
    returns None without a request until it expires.
  * A timeout, a connection error, a malformed body or a payload missing the
    block it promised all return None.
  * Nothing raises. A shift with a gap in its timeline is a worse record than one
    without a gap and an infinitely better outcome than a scheduler that stopped.

The cache is a plain module-level dict, which means it is per worker process and
does not survive a restart. That is the right trade for this: a cold cache costs
one extra request per place, and the alternative — a redis key or a table — is a
new failure mode in the path whose entire job is not to have any.
"""

from __future__ import annotations

import math
import time

import frappe

from .. import compat, shifts

SETTINGS_DOCTYPE = "Weather Settings"
OVERRIDE_DOCTYPE = "Weather Company Override"

#: What goes in `Farm Shift Weather Reading.source`. An auditor is entitled to
#: know whether a temperature was measured at the block or modelled for the grid
#: square, and — separately — whether it was fetched while the shift was running
#: or reconstructed from the archive months later. These two strings are how the
#: row says so on its own face.
SOURCE_CURRENT = "Open-Meteo forecast API"
SOURCE_ARCHIVE = "Open-Meteo archive API"

#: The compliance event a crossing writes. One of the shipped options on
#: `Farm Shift Compliance Event.event_type`, and the only one nobody logs by hand.
THRESHOLD_EVENT = "Threshold Crossed"

#: The shift type wind is read against. Fifteen miles an hour during a prune is
#: weather; during an application it is a decision somebody has to make, and
#: nearly every pesticide label sets a maximum.
SPRAY_SHIFT_TYPE = "Spray"

#: Shipped defaults, restated here so a site whose Weather Settings doctype has
#: not migrated yet still reads sensible numbers rather than zeroes. `_setting`
#: prefers the stored value, then the DocType's own declared default, then these.
DEFAULTS = {
	"enabled": 1,
	"fetch_interval_minutes": 15,
	"heat_threshold_temp_f": 80.0,
	"heat_threshold_heat_index_f": 80.0,
	"wind_threshold_mph_spray_block": 15.0,
	"open_meteo_base_url_current": "https://api.open-meteo.com/v1/forecast",
	"open_meteo_base_url_archive": "https://archive-api.open-meteo.com/v1/archive",
	"open_meteo_base_url_geocoding": "https://geocoding-api.open-meteo.com/v1/search",
	"cache_ttl_seconds": 300,
	"http_timeout_seconds": 10,
}

#: The hourly variables the archive API is asked for, and the current-conditions
#: variables the forecast API is asked for. Same list in both spellings because
#: Open-Meteo names them identically across the two hosts, which is the one thing
#: that makes a single normaliser possible.
VARIABLES = (
	"temperature_2m",
	"apparent_temperature",
	"relative_humidity_2m",
	"wind_speed_10m",
	"wind_direction_10m",
	"precipitation",
)

#: PRECIPITATION IS REQUESTED IN MILLIMETRES AND NOT IN INCHES, because the column
#: it lands in is called `precipitation_mm` and says mm on the form. A unit that
#: disagrees with its own label is the kind of error that survives for years and
#: is discovered by somebody comparing a rain figure against a gauge. Temperature
#: and wind ARE converted, because those columns say °F and mph.
UNITS = {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph"}

#: How long the first backoff lasts, and the ceiling it doubles towards. A 429 is
#: a request to stop asking; an integration that answers one by retrying
#: immediately is one whose address ends up blocked rather than throttled.
BACKOFF_START_SECONDS = 60
BACKOFF_CAP_SECONDS = 3600

#: Most shifts one sweep will walk. A farm with more than two hundred crews open
#: at once has a data problem rather than a weather problem, and a sweep that
#: tried to fetch for all of them would hold a scheduler worker for minutes.
SWEEP_CAP = 200

#: Most readings one archive backfill will append. Ten days of hourly readings on
#: a single shift is already a shift somebody mis-entered the end time on.
BACKFILL_CAP = 500

#: `(lat, lon, kind)` → `(expires_at, payload)`. Per worker process, deliberately.
_CACHE: dict = {}

#: `scope` → `(retry_after_epoch, next_wait_seconds)`. Keyed by coordinate for a
#: reading and by place name for a geocode, so one unlucky block does not stop
#: the sweep fetching for every other one.
_BACKOFF: dict = {}


def _clock() -> float:
	"""Wall-clock seconds. A function so a test can freeze it and age the cache."""
	return time.time()


def reset_cache() -> None:
	"""Forget every cached answer and every backoff.

	Public because two callers need it and both are legitimate: a test that would
	otherwise inherit the previous test's cache, and `fetch_weather_now`, whose
	entire purpose is to ask again right now — a foreman standing in a block
	saying it is hotter than the record shows is not served by a five-minute-old
	answer.
	"""
	_CACHE.clear()
	_BACKOFF.clear()


# ── settings ────────────────────────────────────────────────────────────────
def settings_doc():
	"""The Weather Settings single, or None on a site that has not migrated it."""
	try:
		return frappe.get_cached_doc(SETTINGS_DOCTYPE)
	except Exception:
		return None


def _setting(fieldname: str):
	"""One setting: the stored value, then the declared default, then `DEFAULTS`.

	MISSING IS NOT ZERO, for the same reason `settings.py` says missing is not
	off. A Frappe Single stores one row per field somebody has saved, so on a
	fresh install every field reads None — and a timeout of None becomes a timeout
	of zero one `int()` later, which fails every connection immediately and
	silently. The DocType's own declared default is the first fallback and
	`DEFAULTS` is the second, so a site mid-migrate still fetches.
	"""
	doc = settings_doc()
	if doc is not None:
		value = doc.get(fieldname)
		if value not in (None, ""):
			return value
	try:
		field = frappe.get_meta(SETTINGS_DOCTYPE).get_field(fieldname)
		if field is not None and field.default not in (None, ""):
			return field.default
	except Exception:
		pass
	return DEFAULTS.get(fieldname)


def _number(fieldname: str, cast, floor=None):
	try:
		value = cast(_setting(fieldname))
	except (TypeError, ValueError):
		value = cast(DEFAULTS.get(fieldname) or 0)
	if floor is not None and value < floor:
		value = cast(DEFAULTS.get(fieldname) or floor)
	return value


def enabled() -> bool:
	"""The kill switch. Off means no outbound request from this site, at all."""
	from .. import settings as mcp_settings

	if not compat.doctype_exists(SETTINGS_DOCTYPE):
		# The doctype has not migrated yet. Weather is off rather than defaulted
		# on: a site that cannot store the configuration cannot be said to have
		# consented to the requests.
		return False
	return mcp_settings.as_bool(_setting("enabled"))


def cache_ttl_seconds() -> int:
	return _number("cache_ttl_seconds", int, floor=1)


def http_timeout_seconds() -> int:
	return _number("http_timeout_seconds", int, floor=1)


def fetch_interval_minutes() -> int:
	return _number("fetch_interval_minutes", int, floor=1)


def base_url(kind: str) -> str:
	return str(_setting(f"open_meteo_base_url_{kind}") or DEFAULTS[f"open_meteo_base_url_{kind}"]).strip()


def thresholds_for(company: str = "") -> dict:
	"""The three numbers this company's readings are read against.

	THE COMPANY OVERRIDE WINS FIELD BY FIELD, not row by row. A row that exists to
	lower one entity's wind limit leaves its heat limits at the parent's numbers,
	because a null on the override means "no opinion" and not "zero" — and a zero
	heat threshold would make every reading on that entity a crossing, which is
	the same as making none of them mean anything.

	There is no per-SHIFT override and that is deliberate rather than pending. A
	threshold is a policy about an operation, not a property of one afternoon; a
	shift that could carry its own would be a shift whose foreman could raise the
	bar on the hot day, which is precisely the day the number exists for.
	"""
	out = {
		"heat_threshold_temp_f": float(_number("heat_threshold_temp_f", float, floor=0.0)),
		"heat_threshold_heat_index_f": float(_number("heat_threshold_heat_index_f", float, floor=0.0)),
		"wind_threshold_mph_spray_block": float(_number("wind_threshold_mph_spray_block", float, floor=0.0)),
		"company": company or None,
		"overridden": [],
	}
	company = str(company or "").strip()
	if not company:
		return out
	for row in override_rows():
		if str(row.get("company") or "").strip() != company:
			continue
		for key in (
			"heat_threshold_temp_f",
			"heat_threshold_heat_index_f",
			"wind_threshold_mph_spray_block",
		):
			value = row.get(key)
			if value in (None, ""):
				continue
			try:
				out[key] = float(value)
			except (TypeError, ValueError):
				continue
			out["overridden"].append(key)
		# ONE ROW PER COMPANY. The controller refuses a second, so the first match
		# is the only match — and stopping here rather than merging every row is
		# what makes that refusal load-bearing instead of decorative.
		break
	return out


def override_rows() -> list:
	"""The per-company threshold rows, read off the CACHED Single.

	Not through `frappe.db.get_all`, and the reason is cost rather than taste:
	`thresholds_for` is called once per reading per sweep, and `get_cached_doc` on
	a Single is a redis hit the framework invalidates on save. A query per reading
	would be a query per shift per fifteen minutes for a handful of rows that
	change about once a season.
	"""
	doc = settings_doc()
	if doc is None:
		return []
	rows = doc.get("per_company_overrides") or []
	return [row for row in rows if isinstance(row, dict)]


# ── the heat index ──────────────────────────────────────────────────────────
def heat_index_f(temp_f, humidity_pct):
	"""The NWS heat index, in °F, or None where either input is missing.

	The Rothfusz regression with the two published adjustments, and the low-heat
	simple form below 80 °F where Rothfusz is known to be wrong. This is not a
	convenience: it is the number OAR 437-004-1131 engages at, and computing it
	from the observation rather than reading `apparent_temperature` off the API is
	what makes the reading and the rule talk about the same quantity.

	88 °F at 70 % relative humidity is 100 °F here, which is the worked example in
	the Farm Shift Weather Reading doctype's own description of the field.
	"""
	try:
		temp = float(temp_f)
		humidity = float(humidity_pct)
	except (TypeError, ValueError):
		return None
	if humidity < 0 or humidity > 100:
		return None

	# Steadman's simple form first. Below about 80 °F the full regression is a
	# poor fit, and the NWS's own guidance is to use the average of the two and
	# only escalate when it clears 80.
	simple = 0.5 * (temp + 61.0 + ((temp - 68.0) * 1.2) + (humidity * 0.094))
	if (simple + temp) / 2.0 < 80.0:
		return round(simple, 1)

	index = (
		-42.379
		+ 2.04901523 * temp
		+ 10.14333127 * humidity
		- 0.22475541 * temp * humidity
		- 0.00683783 * temp * temp
		- 0.05481717 * humidity * humidity
		+ 0.00122874 * temp * temp * humidity
		+ 0.00085282 * temp * humidity * humidity
		- 0.00000199 * temp * temp * humidity * humidity
	)
	if humidity < 13 and 80.0 <= temp <= 112.0:
		index -= ((13.0 - humidity) / 4.0) * math.sqrt((17.0 - abs(temp - 95.0)) / 17.0)
	elif humidity > 85 and 80.0 <= temp <= 87.0:
		index += ((humidity - 85.0) / 10.0) * ((87.0 - temp) / 5.0)
	return round(index, 1)


# ── the HTTP layer ──────────────────────────────────────────────────────────
def _log(message: str) -> None:
	"""Say what went wrong, without ever being the thing that goes wrong.

	`frappe.log_error` writes an Error Log row, which is a database write, which
	is a thing that can fail on the site where this is already failing. So the
	logger is wrapped too. A weather fetch that could not even record why it did
	not happen still must not take the sweep down.
	"""
	try:
		frappe.log_error(title="erpnext_mcp: weather", message=message)
	except Exception:  # pragma: no cover - a site that cannot write its own Error Log
		pass


def _backed_off(scope: str) -> bool:
	entry = _BACKOFF.get(scope)
	if not entry:
		return False
	until, _wait = entry
	if _clock() >= until:
		_BACKOFF.pop(scope, None)
		return False
	return True


def _start_backoff(scope: str) -> int:
	"""Double this scope's wait, capped at an hour, and return the new wait."""
	_previous_until, wait = _BACKOFF.get(scope, (0.0, 0))
	wait = min(BACKOFF_CAP_SECONDS, (wait * 2) if wait else BACKOFF_START_SECONDS)
	_BACKOFF[scope] = (_clock() + wait, wait)
	return wait


def backoff_seconds_remaining(scope: str) -> int:
	"""How long this scope is still refusing to ask, in seconds. 0 when it is not.

	Reported by `fetch_weather_now` rather than kept private, because "Open-Meteo
	asked us to stop for another eleven minutes" is an answer a foreman can act on
	and "no reading was available" is not.
	"""
	entry = _BACKOFF.get(scope)
	if not entry:
		return 0
	remaining = entry[0] - _clock()
	return int(remaining) if remaining > 0 else 0


def _get_json(url: str, params: dict, scope: str):
	"""One GET. Returns the decoded body, or None for every possible failure.

	NEVER RAISES. Every branch here is a real thing Open-Meteo has done to
	somebody: a timeout on a slow link out of a farm office, a 429 on a bench that
	restarted its workers and lost the cache, a 502 from a CDN, and a 200 carrying
	an HTML error page from a captive portal that intercepted the request.
	"""
	if not str(url or "").lower().startswith(("http://", "https://")):
		_log(f"refusing to fetch {url!r}: not an http(s) URL. Check Weather Settings.")
		return None
	if _backed_off(scope):
		return None
	try:
		import requests
	except Exception:  # pragma: no cover - Frappe depends on requests
		_log("the `requests` package is not importable, so no weather can be fetched.")
		return None

	try:
		response = requests.get(url, params=params, timeout=http_timeout_seconds())
	except Exception as exc:
		_log(f"{url} did not answer: {type(exc).__name__}: {exc}")
		return None

	status = int(getattr(response, "status_code", 0) or 0)
	if status == 429 or status >= 500:
		wait = _start_backoff(scope)
		_log(
			f"{url} answered {status}. Backing off {wait}s for {scope!r} — a rate limit answered "
			"by retrying immediately is how this site gets blocked rather than throttled."
		)
		return None
	if status >= 400:
		_log(f"{url} answered {status}: {str(getattr(response, 'text', ''))[:500]}")
		return None

	try:
		payload = response.json()
	except Exception as exc:
		_log(f"{url} answered {status} with a body that is not JSON: {type(exc).__name__}: {exc}")
		return None
	if not isinstance(payload, dict):
		_log(f"{url} answered with {type(payload).__name__}, not an object.")
		return None
	# A successful answer clears the backoff: the far end has evidently forgiven
	# us, and continuing to refuse to ask would be this app rate-limiting itself.
	_BACKOFF.pop(scope, None)
	return payload


def _cache_key(lat: float, lon: float, kind: str) -> tuple:
	"""Rounded to four decimals — about eleven metres, which is the same block.

	Two shifts on adjacent rows of the same orchard resolve to the same key and
	share one request, which is the entire point. Four decimals is as fine as the
	distinction is worth: Open-Meteo models a grid square measured in kilometres,
	so asking it twice about two points eleven metres apart returns the same
	number twice.
	"""
	return (round(float(lat), 4), round(float(lon), 4), kind)


def _cached(key: tuple):
	entry = _CACHE.get(key)
	if not entry:
		return None
	expires, payload = entry
	if _clock() >= expires:
		_CACHE.pop(key, None)
		return None
	return payload


def _remember(key: tuple, payload):
	_CACHE[key] = (_clock() + cache_ttl_seconds(), payload)
	return payload


# ── location ────────────────────────────────────────────────────────────────
def parse_coordinates(text: str):
	"""`(lat, lon)` out of whatever somebody typed, or None if it is a place name.

	Three spellings arrive in practice and all three are accepted, because the
	field is free text on purpose and refusing one of them means a shift with no
	weather rather than a shift with a tidier string:

	    45.52,-122.68
	    45.52, -122.68
	    Latitude 45.52, longitude -122.68     ← the spelling the doctype documents

	A pair outside the real ranges is rejected rather than clamped. -122.68,45.52
	is a transposition somebody will make, and it is a real place in the Yellow
	Sea; fetching weather for it and filing the answer against an Oregon shift
	would be a fabricated record that looks exactly like a true one.
	"""
	raw = str(text or "").strip()
	if not raw:
		return None
	cleaned = raw.lower()
	for word in ("latitude", "longitude", "lat", "lon", "lng", "°", ":", ";"):
		cleaned = cleaned.replace(word, " ")
	parts = [chunk.strip() for chunk in cleaned.replace(",", " ").split() if chunk.strip()]
	if len(parts) != 2:
		return None
	try:
		lat, lon = float(parts[0]), float(parts[1])
	except (TypeError, ValueError):
		return None
	if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
		return None
	return (lat, lon)


def resolve_location(farm_location_gps: str):
	"""`(lat, lon)` for a shift's location string, or None.

	Coordinates are parsed directly and never leave this site. A PLACE NAME COSTS
	ONE GEOCODING REQUEST AND IS THEN CACHED, because a block does not move — and
	because the alternative, refusing anything that is not a number pair, would
	mean the operations most likely to type "Milton-Freewater, OR" get no timeline
	at all.

	AMBIGUITY IS RESOLVED BY TAKING THE FIRST RESULT AND SAYING SO. Open-Meteo's
	geocoder is asked for one match and returns the most populous place with that
	name, which is right for "Milton-Freewater, OR" and wrong for a block somebody
	called "Home". There is no way to be sure from a string, so the honest fix is
	the one the tools take: `get_weather_settings` and `fetch_weather_now` report
	the coordinates a name resolved to, so a foreman who sees Ohio can put the
	numbers on the shift instead.
	"""
	raw = str(farm_location_gps or "").strip()
	if not raw:
		return None
	direct = parse_coordinates(raw)
	if direct:
		return direct

	key = ("geocode", raw.lower(), "geocode")
	cached = _cached(key)
	if cached is not None:
		return cached or None

	payload = _get_json(
		base_url("geocoding"),
		{"name": raw, "count": 1, "format": "json"},
		scope=f"geocode:{raw.lower()}",
	)
	if payload is None:
		return None
	results = payload.get("results") or []
	if not results or not isinstance(results[0], dict):
		# CACHED AS A NEGATIVE. A name the geocoder does not know will not start
		# being known within the cache window, and asking again on every tick of
		# every sweep is how a free service stops answering this site at all.
		_remember(key, ())
		return None
	first = results[0]
	try:
		found = (float(first.get("latitude")), float(first.get("longitude")))
	except (TypeError, ValueError):
		_remember(key, ())
		return None
	_remember(key, found)
	return found


# ── normalising ─────────────────────────────────────────────────────────────
def _reading(when, temp, apparent, humidity, wind, direction, precipitation, source, raw):
	"""One row in the shape `Farm Shift Weather Reading` stores.

	`fetched_at` is now and `reading_datetime` is when the conditions were true,
	and conflating them is the thing the doctype's own description warns about: a
	backfilled reading is a true statement about 14:15 last July whenever it was
	written, and a row that said otherwise would make every backfilled shift look
	documented after the fact.
	"""

	def number(value):
		try:
			return None if value is None else float(value)
		except (TypeError, ValueError):
			return None

	# `2026-07-24T06:00` → `2026-07-24 06:00:00`. BOTH HALVES MATTER. Open-Meteo
	# answers ISO-8601 with a `T` and no seconds; a Frappe Datetime column wants
	# a space and wants the seconds, and a string one character short of the
	# format is a value that sorts wrong against every other reading on the
	# timeline and never matches the shift's own start to the second.
	stamp = str(when or "").replace("T", " ")[:19]
	if len(stamp) == 16:
		stamp = f"{stamp}:00"

	temp_f = number(temp)
	humidity_pct = number(humidity)
	index = heat_index_f(temp_f, humidity_pct)
	if index is None:
		# NO HUMIDITY MEANS NO HEAT INDEX, and `apparent_temperature` is the least
		# bad stand-in — it is a different quantity, so it is used only where the
		# right one cannot be computed and the source column still says where the
		# whole row came from.
		index = number(apparent)
	return {
		"reading_datetime": stamp or None,
		"temp_f": temp_f,
		"heat_index_f": index,
		"humidity_pct": humidity_pct,
		"wind_speed_mph": number(wind),
		"wind_direction_deg": number(direction),
		"precipitation_mm": number(precipitation),
		"source": source,
		"fetched_at": frappe.utils.now(),
		"raw_response": raw,
	}


def fetch_current(lat: float, lon: float, use_cache: bool = True):
	"""Conditions at this place, now. A normalised dict, or None on any failure.

	`current=` rather than `minutely_15=`. The sweep's cadence IS fifteen minutes,
	so the current block is the reading at the moment of asking and a fifteen-
	minute series would return that same instant plus a run of forecasts this app
	does not store. The parameter is there when a finer live cadence is ever
	wanted; asking for it today would be requesting data to throw away.
	"""
	try:
		lat, lon = float(lat), float(lon)
	except (TypeError, ValueError):
		return None
	key = _cache_key(lat, lon, "current")
	if use_cache:
		cached = _cached(key)
		if cached is not None:
			return dict(cached) if cached else None

	payload = _get_json(
		base_url("current"),
		{
			"latitude": lat,
			"longitude": lon,
			"current": ",".join(VARIABLES),
			**UNITS,
		},
		scope=f"{round(lat, 4)},{round(lon, 4)}",
	)
	if payload is None:
		return None
	current = payload.get("current")
	if not isinstance(current, dict):
		_log(f"the forecast API answered for {lat},{lon} with no `current` block: {str(payload)[:400]}")
		return None

	reading = _reading(
		current.get("time"),
		current.get("temperature_2m"),
		current.get("apparent_temperature"),
		current.get("relative_humidity_2m"),
		current.get("wind_speed_10m"),
		current.get("wind_direction_10m"),
		current.get("precipitation"),
		SOURCE_CURRENT,
		payload,
	)
	if not reading["reading_datetime"]:
		# The API did not say WHEN, so this app says it: a reading with no
		# timestamp cannot be deduplicated, cannot be matched to a compliance
		# event, and cannot be read as a timeline.
		reading["reading_datetime"] = frappe.utils.now()[:19]
	_remember(key, dict(reading))
	return reading


def fetch_archive(lat: float, lon: float, start, end):
	"""Every hourly reading between two instants. A list, or None on failure.

	HOURLY IS THE ARCHIVE'S OWN GRANULARITY and this does not pretend otherwise.
	A live shift gets a reading every fifteen minutes and a backfilled one gets
	four times fewer, which is a real difference between contemporaneous evidence
	and reconstruction — and one the `source` column on every row states, so
	nobody reads a backfilled timeline as something it is not.
	"""
	try:
		lat, lon = float(lat), float(lon)
	except (TypeError, ValueError):
		return None
	start_date = str(start or "")[:10]
	end_date = str(end or "")[:10]
	if not start_date or not end_date:
		return None

	key = _cache_key(lat, lon, f"archive:{start_date}:{end_date}")
	cached = _cached(key)
	if cached is not None:
		return [dict(row) for row in cached]

	payload = _get_json(
		base_url("archive"),
		{
			"latitude": lat,
			"longitude": lon,
			"start_date": start_date,
			"end_date": end_date,
			"hourly": ",".join(VARIABLES),
			**UNITS,
		},
		scope=f"{round(lat, 4)},{round(lon, 4)}",
	)
	if payload is None:
		return None
	hourly = payload.get("hourly")
	if not isinstance(hourly, dict):
		_log(f"the archive API answered for {lat},{lon} with no `hourly` block: {str(payload)[:400]}")
		return None

	times = hourly.get("time") or []
	if not isinstance(times, list):
		return None

	def series(name):
		values = hourly.get(name)
		return values if isinstance(values, list) else []

	temps = series("temperature_2m")
	apparent = series("apparent_temperature")
	humidity = series("relative_humidity_2m")
	wind = series("wind_speed_10m")
	direction = series("wind_direction_10m")
	precipitation = series("precipitation")

	def at(values, index):
		return values[index] if index < len(values) else None

	out = []
	for index, when in enumerate(times[:BACKFILL_CAP]):
		out.append(
			_reading(
				when,
				at(temps, index),
				at(apparent, index),
				at(humidity, index),
				at(wind, index),
				at(direction, index),
				at(precipitation, index),
				SOURCE_ARCHIVE,
				None,
			)
		)
	_remember(key, [dict(row) for row in out])
	return out


# ── writing readings ────────────────────────────────────────────────────────
def existing_reading_times(shift: str) -> set:
	"""Every `reading_datetime` already on this shift, to the minute.

	TO THE MINUTE AND NOT TO THE SECOND, because the archive answers on the hour
	with a seconds field of zero and the live fetch answers with whatever the API
	rounded to, and a backfill run over a shift that was also swept live must not
	produce two rows for one instant. Deduplicating on the minute is what makes
	`backfill_weather_for_shift` genuinely idempotent rather than idempotent only
	against its own previous run.
	"""
	return {str(row.get("reading_datetime") or "")[:16] for row in shifts.weather_of(shift)}


def append_readings(shift_name: str, readings: list) -> dict:
	"""Append the readings this shift does not already have. Never edits one.

	A weather reading is an immutable evidence record. A row whose numbers changed
	after the fact is a row an auditor is entitled to disbelieve entirely, and the
	whole defensive value of a timeline is that it was written as things happened.
	So a reading whose minute is already present is SKIPPED and reported, never
	merged over the top of what is there.
	"""
	report = {"added": 0, "skipped": 0, "failed": 0, "appended": [], "note": ""}
	readings = [row for row in (readings or []) if row and row.get("reading_datetime")]
	if not readings:
		return report
	try:
		doc = frappe.get_doc(shifts.DOCTYPE, shift_name)
	except Exception as exc:
		report["failed"] = len(readings)
		report["note"] = f"{shifts.DOCTYPE} {shift_name} could not be loaded: {type(exc).__name__}: {exc}"
		return report

	seen = existing_reading_times(shift_name)
	for reading in readings:
		minute = str(reading.get("reading_datetime") or "")[:16]
		if minute in seen:
			report["skipped"] += 1
			continue
		seen.add(minute)
		doc.append(
			"weather_timeline",
			{
				"reading_datetime": reading.get("reading_datetime"),
				"temp_f": reading.get("temp_f"),
				"heat_index_f": reading.get("heat_index_f"),
				"humidity_pct": reading.get("humidity_pct"),
				"wind_speed_mph": reading.get("wind_speed_mph"),
				"wind_direction_deg": reading.get("wind_direction_deg"),
				"precipitation_mm": reading.get("precipitation_mm"),
				"source": reading.get("source"),
				"fetched_at": reading.get("fetched_at") or frappe.utils.now(),
			},
		)
		report["added"] += 1
		report["appended"].append(reading)

	if not report["added"]:
		return report
	try:
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
	except Exception as exc:
		report["failed"] = report["added"]
		report["added"] = 0
		report["appended"] = []
		report["note"] = (
			f"{shifts.DOCTYPE} {shift_name} refused the readings: {type(exc).__name__}: {exc}. "
			"The shift itself is untouched."
		)
	return report


# ── thresholds ──────────────────────────────────────────────────────────────
def _crossings(reading: dict, shift: dict, limits: dict) -> list:
	"""Which thresholds this one reading is at or above, in the rule's own words."""
	out = []
	temp = reading.get("temp_f")
	index = reading.get("heat_index_f")
	wind = reading.get("wind_speed_mph")
	when = reading.get("reading_datetime")

	if temp is not None and float(temp) >= limits["heat_threshold_temp_f"]:
		out.append(
			f"Air temperature reached {float(temp):.1f} °F at {when}, at or above the "
			f"{limits['heat_threshold_temp_f']:.0f} °F threshold on Weather Settings."
		)
	if index is not None and float(index) >= limits["heat_threshold_heat_index_f"]:
		out.append(
			f"Heat index reached {float(index):.1f} °F at {when}, at or above the "
			f"{limits['heat_threshold_heat_index_f']:.0f} °F threshold. {shifts.CITATION} engages "
			"here: water at the required rate, shade within reach, the preventative cool-down rest "
			"cycle, and observation for heat illness signs."
		)
	if (
		wind is not None
		and str(shift.get("shift_type") or "") == SPRAY_SHIFT_TYPE
		and float(wind) >= limits["wind_threshold_mph_spray_block"]
	):
		out.append(
			f"Wind reached {float(wind):.1f} mph at {when} on a Spray shift, at or above the "
			f"{limits['wind_threshold_mph_spray_block']:.0f} mph spray-block threshold. Nearly every "
			"pesticide label sets a maximum, and wind speed is the first thing a drift investigation "
			"asks for."
		)
	return out


def _heat_crossing(reading: dict, limits: dict) -> bool:
	temp = reading.get("temp_f")
	index = reading.get("heat_index_f")
	return bool(
		(temp is not None and float(temp) >= limits["heat_threshold_temp_f"])
		or (index is not None and float(index) >= limits["heat_threshold_heat_index_f"])
	)


def already_crossed(shift: str, at_or_before: str) -> bool:
	"""Whether this shift already carries a Threshold Crossed event by this time.

	ONE PER SHIFT, NOT ONE PER READING, and this is the whole of the deduplication.
	A shift that ran nine hours above eighty degrees would otherwise collect
	thirty-six identical events, and a timeline of thirty-six identical rows is
	one nobody reads — which would bury the water breaks and the observations that
	are the evidence somebody actually needs to find.
	"""
	for row in shifts.events_of(shift):
		if str(row.get("event_type") or "") != THRESHOLD_EVENT:
			continue
		when = str(row.get("event_datetime") or "")
		if not when or not at_or_before or when <= str(at_or_before):
			return True
	return False


def heat_announced_for(shift: str, limits: dict) -> bool:
	"""Whether this shift has already logged a Threshold Crossed event ABOUT HEAT. v0.140.0.

	THE SIBLING OF `already_crossed`, AND NOT THE SAME QUESTION. That one asks
	whether the shift carries any Threshold Crossed event at all, which is the
	right fence for the timeline: one row per shift is what stops nine hours above
	eighty degrees becoming thirty-six identical rows. It is the WRONG fence for
	the horn, because of a case that reads like a corner and is not: a Spray shift
	crosses the WIND threshold at 09:00 in cool air, gets its one event, and then
	the afternoon turns hot. `already_crossed` says yes, the crossing was recorded
	— and the foreman's phone would never ring on the hottest day of the season,
	for a reason nobody would ever find.

	SO THIS READS THE STORED NUMBERS RATHER THAN THE PROSE. Every Threshold
	Crossed event carries `weather_snapshot_temp_f` and
	`weather_snapshot_heat_index_f` — the reading that produced it — so "was this
	event about heat" is answerable from the columns, against the same limits the
	crossing itself was judged against. Matching on the description text would be
	a fence made of a sentence somebody may reword.
	"""
	for row in shifts.events_of(shift):
		if str(row.get("event_type") or "") != THRESHOLD_EVENT:
			continue
		if _heat_crossing(
			{
				"temp_f": row.get("weather_snapshot_temp_f"),
				"heat_index_f": row.get("weather_snapshot_heat_index_f"),
			},
			limits,
		):
			return True
	return False


def _push_heat_crossing(shift: dict, reading: dict, limits: dict, when: str) -> dict:
	"""Ring the crew leader standing in it. Never raises. v0.140.0.

	WHO. THE SHIFT'S OWN FOREMAN, and this is the one push in this app that is
	addressed to a single named person rather than to a role or to a crew.
	`alert_payload`'s comment argues that a compliance alert goes to supervisors
	because the subject cannot act on it and the employer can; the same reasoning
	lands somewhere else here. The obligation OAR 437-004-1131 creates — water at
	the required rate, shade within reach, the preventative cool-down cycle,
	observation for heat illness signs — belongs to the person who is standing on
	the block, and the compliance rule of the same name already says so in code:
	`producer_assigned_to_expression` is `row.foreman`. A push to the office would
	reach people who cannot call a break in a field they are not in.

	A SHIFT WITH NO FOREMAN FALLS BACK TO THE SUPERVISORS AND SAYS SO. That is an
	imported or hand-written shift rather than one `start_shift` produced, and the
	choice there is between the wrong recipients and nobody at all. `recipients`
	in the report names which of the two happened, because "it rang the office"
	and "it rang the crew leader" are different states of the same farm.

	Never raises, for the reason every other entry point in this module does not:
	this runs inside a cron sweep with nobody watching, and a notification that
	could not be sent must never be able to cost the weather reading or the
	timeline event that were already written by the time it runs.
	"""
	report = {"sent": 0, "reason": "", "recipients": ""}
	try:
		from . import push as push_service

		foreman = str(shift.get("foreman") or "").strip()
		if foreman:
			recipients = [foreman]
			report["recipients"] = "foreman"
		else:
			recipients = push_service.supervisor_employees(str(shift.get("company") or ""))
			report["recipients"] = "supervisors"
		report["foreman"] = foreman or None

		payload = push_service.heat_payload(
			shift=str(shift.get("name") or ""),
			location=str(shift.get("location") or ""),
			temp_f=reading.get("temp_f"),
			heat_index_f=reading.get("heat_index_f"),
			reading_datetime=str(reading.get("reading_datetime") or "") or str(when or ""),
			threshold_temp_f=limits.get("heat_threshold_temp_f"),
			threshold_heat_index_f=limits.get("heat_threshold_heat_index_f"),
		)
		answer = push_service.send_push_to_employees(
			recipients,
			payload,
			# PRIORITY 10, unlike the compliance sweep's 5. A crew is in the sun
			# now; a battery saved by holding this until the phone next wakes is
			# saved at the cost of the thing the notification is for.
			priority=push_service.PRIORITY_IMMEDIATE,
			# The shift docname. A foreman with two handsets gets one notification,
			# and a resend — which the fence above makes rare and does not make
			# impossible — replaces its predecessor rather than stacking.
			collapse_id=str(shift.get("name") or ""),
		)
		report.update({key: answer.get(key) for key in ("employees", "tokens", "sent", "failed", "skipped")})
		report["reason"] = str(answer.get("reason") or "")
	except Exception as exc:  # pragma: no cover - send_push_to_employees is itself wrapped
		report["reason"] = f"error: {exc}"
		_log(f"heat push for {shift.get('name')!r} failed: {type(exc).__name__}: {exc}")
	return report


def evaluate_thresholds(shift: dict, reading: dict) -> dict:
	"""Log the crossing if this reading is the first one, ring the foreman, align the record.

	THREE THINGS HAPPEN AND A FOURTH DELIBERATELY DOES NOT.

	It writes a Threshold Crossed compliance event, because "the heat index
	reached 84 °F at 11:45" is arithmetic over a stored reading and needs nobody's
	judgement. It PUSHES to the crew leader on a heat crossing — v0.140.0, and see
	`_push_heat_crossing` for why that one named person and not the office — once
	per shift, because until then the timeline said it was 84 °F at 11:45 and the
	only person who could act on that learned it by opening the app. It moves an
	existing Heat Exposure Event's `threshold_crossed_at` EARLIER where this
	reading proves the crossing happened before the time on the record, because
	the earliest crossing is the one -1131's obligations run from and a later time
	understates the exposure period.

	IT NEVER CREATES A HEAT EXPOSURE EVENT. That record names which crew was
	exposed, what water was provided, whether the rest cycle was taken and whether
	anybody showed signs, and it carries a supervisor's signature. Every one of
	those is a judgement by the person who was standing there. A record filed
	automatically from a temperature would be an attestation with nobody behind
	it, in the one place where having nobody behind it is the whole failure.
	"""
	out = {"crossed": [], "event_logged": False, "heat_event_updated": None}
	name = str(shift.get("name") or "")
	if not name:
		return out
	limits = thresholds_for(str(shift.get("company") or ""))
	reasons = _crossings(reading, shift, limits)
	if not reasons:
		return out
	out["crossed"] = reasons

	# READ BEFORE THE EVENT IS APPENDED, because the event this call is about to
	# write would itself answer the question. `heat_announced_for` is the horn's
	# one-per-shift fence and it has to be evaluated against the shift as it was
	# when this reading arrived.
	announced_before = heat_announced_for(name, limits)

	when = str(reading.get("reading_datetime") or "") or frappe.utils.now()
	if not already_crossed(name, when):
		try:
			doc = frappe.get_doc(shifts.DOCTYPE, name)
			doc.append(
				"compliance_events",
				{
					"event_type": THRESHOLD_EVENT,
					"event_datetime": when,
					# NO `logged_by`. It is a Link to Employee and nobody logged
					# this — the sweep did. Naming the foreman would put their
					# identity against an observation they did not make, on the
					# one doctype whose value is that it says who did what.
					"description": (
						"Logged automatically from the shift's weather timeline. "
						+ " ".join(reasons)
						+ " The conditions are recorded; what the shift does about them is the "
						"foreman's call, and create_heat_exposure_event is where that is written."
					),
					"weather_snapshot_temp_f": reading.get("temp_f"),
					"weather_snapshot_heat_index_f": reading.get("heat_index_f"),
					"producer_record_doctype": SETTINGS_DOCTYPE,
					"producer_record_name": SETTINGS_DOCTYPE,
				},
			)
			doc.flags.ignore_permissions = True
			doc.save(ignore_permissions=True)
			out["event_logged"] = True
		except Exception as exc:
			_log(f"could not log a {THRESHOLD_EVENT} event on {name}: {type(exc).__name__}: {exc}")

	if _heat_crossing(reading, limits):
		out["heat_event_updated"] = _align_heat_event(name, when)
		# WIND IS NOT PUSHED AND HEAT IS. A spray block over the wind threshold is
		# a reason to stop spraying, which the applicator standing in it already
		# knows from the boom; heat is the one where the condition is survivable
		# minute to minute and the obligation is not, and where -1131 runs a clock
		# from the crossing. Gated on `_heat_crossing` rather than on `reasons`
		# being non-empty for exactly that reason.
		if not announced_before:
			out["heat_push"] = _push_heat_crossing(shift, reading, limits, when)
	return out


def _align_heat_event(shift: str, when: str):
	"""Move an existing heat record's `threshold_crossed_at` earlier. Never later.

	EARLIER ONLY. The time the obligations run from is the FIRST moment the shift
	crossed, so a reading proving an earlier crossing corrects the record and a
	reading proving a later one is simply a later reading. Overwriting with the
	latest would shorten the documented exposure period every fifteen minutes,
	which is the direction that flatters the operation and the direction an
	investigator would be right to disbelieve.

	A record somebody submitted is left alone: it is signed, and an automated
	edit to a signed attestation is exactly the thing signatures exist to prevent.
	"""
	try:
		rows = shifts.heat_rows({"farm_shift": shift}, limit=2)
	except Exception:
		return None
	for row in rows or []:
		if int(row.get("docstatus") or 0) != 0:
			continue
		current = str(row.get("threshold_crossed_at") or "")
		if current and current <= str(when):
			continue
		try:
			frappe.db.set_value(shifts.HEAT_DOCTYPE, row["name"], "threshold_crossed_at", when)
		except Exception as exc:  # pragma: no cover - a half-migrated site
			_log(f"could not align {row['name']}: {type(exc).__name__}: {exc}")
			return None
		return {"heat_exposure_event": row["name"], "threshold_crossed_at": when, "was": current or None}
	return None


# ── one shift ───────────────────────────────────────────────────────────────
def fetch_for_shift(shift: dict, use_cache: bool = True) -> dict:
	"""One live reading for one open shift, appended, with its thresholds evaluated.

	The unit the sweep repeats and the unit `fetch_weather_now` runs once. Never
	raises: every refusal is a sentence in the returned report, because the caller
	is either a scheduler that must keep going or a tool that has to explain
	itself.
	"""
	name = str(shift.get("name") or "")
	report = {
		"shift": name,
		"fetched": False,
		"added": 0,
		"skipped": 0,
		"crossed": [],
		"event_logged": False,
		"reading": None,
		"reason": "",
	}
	place = str(shift.get("farm_location_gps") or "").strip()
	if not place:
		report["reason"] = (
			"This shift has no farm_location_gps, so there is no place to ask about. The service "
			"asks Open-Meteo what the conditions are HERE, and a shift with no coordinates gets no "
			"timeline — set them while the shift is still open."
		)
		return report

	coordinates = resolve_location(place)
	if not coordinates:
		waiting = backoff_seconds_remaining(f"geocode:{place.lower()}")
		report["reason"] = (
			f"{place!r} could not be resolved to coordinates"
			+ (f"; the geocoder is backed off for another {waiting}s" if waiting else "")
			+ ". A latitude,longitude pair on the shift always works and never costs a request."
		)
		return report

	lat, lon = coordinates
	report["coordinates"] = {"latitude": lat, "longitude": lon, "resolved_from": place}
	reading = fetch_current(lat, lon, use_cache=use_cache)
	if not reading:
		waiting = backoff_seconds_remaining(f"{round(lat, 4)},{round(lon, 4)}")
		report["reason"] = (
			"Open-Meteo returned nothing usable for this place"
			+ (f" and is backed off for another {waiting}s" if waiting else "")
			+ ". Weather is best effort: the shift is untouched and the next sweep tries again."
		)
		return report

	report["fetched"] = True
	report["reading"] = reading
	appended = append_readings(name, [reading])
	report["added"] = appended["added"]
	report["skipped"] = appended["skipped"]
	if appended.get("note"):
		report["reason"] = appended["note"]
	if not appended["added"]:
		return report

	crossings = evaluate_thresholds(shift, reading)
	report["crossed"] = crossings["crossed"]
	report["event_logged"] = crossings["event_logged"]
	report["heat_event_updated"] = crossings["heat_event_updated"]
	# v0.140.0. PRESENT ONLY WHEN A HORN WAS ATTEMPTED, which is what makes its
	# absence readable: a hot shift with no `heat_push` key is one that had
	# already crossed, and a hot shift with `heat_push: {"sent": 0, "reason":
	# "no_tokens"}` is a foreman who never enrolled a handset. A zero under a key
	# that is always there could not tell those two apart, and they are different
	# people to go and see.
	if crossings.get("heat_push") is not None:
		report["heat_push"] = crossings["heat_push"]
	return report


def _due(shift_name: str, interval_minutes: int) -> bool:
	"""Whether this shift's newest reading is older than the configured interval.

	THE CRON CANNOT BE CHANGED FROM A FORM. A Frappe cron expression is a static
	string in `hooks.py`, so the schedule runs every fifteen minutes on every site
	whatever `fetch_interval_minutes` says — and the setting is honoured HERE, by
	skipping a shift whose newest reading is younger than the interval. Which
	means raising it to sixty works exactly as an operator expects and lowering it
	to five changes nothing, because the sweep cannot run more often than it is
	scheduled. Raising it is the change operations actually ask for.
	"""
	readings = shifts.weather_of(shift_name)
	if not readings:
		return True
	newest = max(str(row.get("reading_datetime") or "") for row in readings)
	if not newest:
		return True
	try:
		age = float(frappe.utils.time_diff_in_seconds(frappe.utils.now(), newest))
	except Exception:
		return True
	# A reading timestamped in the future is a clock disagreement between this
	# site and the API, not a reason to stop fetching for the rest of the shift.
	if age < 0:
		return True
	return age >= interval_minutes * 60


def open_shifts() -> list:
	"""Every shift that is still running and has a place to ask about.

	FILTERED ON `end_datetime` RATHER THAN ON `status`. The two agree on every
	site where nothing has gone wrong, and `status` is a stored column recomputed
	on save — so a shift saved before somebody changed something, or written by an
	import that set the column directly, could read Closed while still being
	worked. The absence of an end time is the fact; the status is the summary of
	it, and the sweep reads the fact.
	"""
	if not compat.doctype_exists(shifts.DOCTYPE):
		return []
	return shifts.rows(
		{"end_datetime": ("is", "not set"), "farm_location_gps": ("is", "set")},
		limit=SWEEP_CAP,
		order_by="start_datetime asc",
	)


def sweep_open_shifts() -> int:
	"""The scheduled job. One reading per due open shift, and the count written.

	NEVER RAISES AND TAKES NO ARGUMENTS. `scheduler_events` calls it bare on a
	cron, on somebody's bench, beside their real work — a job whose signature
	needed an argument would be a TypeError at three in the morning, and a job
	that raised would take the tick down for every other job in it.

	IT IS SAFE TO RUN AT ANY CADENCE, which is what makes the cron a tuning
	decision rather than a correctness one. Running it twice in a minute appends
	nothing the second time: the interval check skips a shift that has just been
	read, the cache answers the request that would have gone out anyway, and the
	minute-level deduplication in `append_readings` refuses a duplicate row even
	if both of those were bypassed. Three independent reasons the same run twice
	is the same as once.
	"""
	try:
		if not enabled():
			return 0
		interval = fetch_interval_minutes()
		written = 0
		for shift in open_shifts():
			try:
				if not _due(str(shift.get("name") or ""), interval):
					continue
				written += fetch_for_shift(shift).get("added") or 0
			except Exception as exc:
				# ONE SHIFT'S FAILURE IS ONE SHIFT'S FAILURE. A block whose
				# coordinates are nonsense, an employee archived mid-shift, a
				# validation this app did not anticipate — none of them is a
				# reason the other twelve crews go undocumented today.
				_log(f"weather sweep skipped {shift.get('name')}: {type(exc).__name__}: {exc}")
		return written
	except Exception:
		_log(f"the weather sweep failed before it started: {compat.traceback_text()}")
		return 0

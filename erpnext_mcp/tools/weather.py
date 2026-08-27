# SPDX-License-Identifier: MIT
"""Five tools over the weather timeline: two that write it, three that read it.

v0.19.4. THE SCHEDULE IS THE MECHANISM AND THESE ARE THE HANDS. A fifteen-minute
cron documents every open shift without anybody asking, which is the whole design
— a timeline is only evidence if it was written while things were happening. What
the cron cannot do is the rest of it:

  * `fetch_weather_now` is for the foreman standing in a block right now, on a
    day that turned, who wants a reading on the record this minute rather than in
    eleven. It bypasses the cache, because a cached answer is exactly what they
    are asking to get past.
  * `backfill_weather_for_shift` is for every shift that ran before the service
    was switched on. A shift with no timeline is not a shift that was compliant
    or non-compliant; it is a shift nobody can say anything about, and Open-Meteo
    still knows what the weather was that day.
  * `list_shifts_missing_weather` is the worklist for the second one, which is
    the difference between a backfill tool somebody uses once and one they can
    finish a season with.
  * `get_weather_timeline` and `get_weather_settings` are the two reads: what a
    shift's conditions were, and what the numbers those conditions get read
    against actually are on this site.

────────────────────────────────────────────────────────────────────────────
THERE IS NO `update_weather_settings`, AND ITS ABSENCE IS DELIBERATE
────────────────────────────────────────────────────────────────────────────

Weather Settings holds three outbound URLs and three compliance thresholds. A
tool that could write them would be a tool that could point this site's weather
at a server of somebody else's choosing, or raise the heat threshold to 200 °F so
that no shift ever crosses it — and either of those is a change an operator would
never find, because the resulting site behaves normally and simply never says
anything is wrong. Both are one sentence away from any model that can call it.

So the configuration surface is the Desk form, where a human types the number and
Frappe's own version trail records who typed it. `get_weather_settings` reads it
back, which is the half that helps and carries none of the risk.

────────────────────────────────────────────────────────────────────────────
THE GUARDS ARE THE SHIFT TOOLS', SHARED RATHER THAN RESTATED
────────────────────────────────────────────────────────────────────────────

`employee.require_hr_role` and `employee.require_company_scope`, imported, plus
the kill switch on Weather Settings. A weather reading is not personnel data, but
the thing it is appended to is: these tools write to a Farm Shift, which names
who was at work and for how long, and appending to an entity's shift you cannot
see is the same mistake as forming one. Reads are scoped the same way, so a
scoped account asking for "every shift missing weather" gets its own entity's.
"""

from __future__ import annotations

from .. import compat, shifts
from ..args import as_date, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from ..services import weather
from . import employee as employee_tool
from . import shifts as shift_tools

DOCTYPE = shifts.DOCTYPE

#: Most shifts one read returns. Same ceiling `list_shifts` uses, and for the
#: same reason: a register is read to answer a question, not to be exported.
RECORD_CAP = 500

#: Most readings one timeline read returns. A ten-hour shift swept every fifteen
#: minutes has forty; a week-long shift somebody forgot to close has six hundred,
#: and that is a shift to fix rather than a payload to render.
TIMELINE_CAP = 500

#: How many readings an hour of shift is expected to carry before
#: `list_shifts_missing_weather` stops calling it thin. ONE PER HOUR, which is
#: the archive's own granularity — so a fully backfilled shift is never reported
#: as missing weather, and a live-swept one clears the bar four times over.
READINGS_PER_HOUR = 1.0


def _require() -> None:
	compat.require_doctype(
		DOCTYPE,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _require_enabled() -> None:
	"""The kill switch, refused with the sentence that says where to turn it on.

	CHECKED SEPARATELY FROM THE TOOL SWITCH, because they answer different
	questions and an operator needs to be able to answer them differently. The
	tool switch is "may the AI do this"; `Weather Settings.enabled` is "does this
	site talk to Open-Meteo at all", and it governs the scheduled sweep as well —
	which no tool switch reaches.
	"""
	if not compat.doctype_exists(weather.SETTINGS_DOCTYPE):
		raise ToolError(
			f"the {weather.SETTINGS_DOCTYPE!r} DocType is not installed on this site. It ships "
			"with erpnext_mcp — run `bench --site <site> migrate` after upgrading. Nothing was "
			"changed."
		)
	if not weather.enabled():
		raise ToolError(
			"weather is switched off on this site: `enabled` is unticked on Weather Settings. "
			"That switch governs the scheduled sweep as well as this call, so nothing is being "
			"appended to any shift's timeline right now. An operator ticks it in the Desk. "
			"Nothing was changed."
		)


def _readable_companies(actor: str) -> list:
	from .. import roles

	return roles.companies_for(actor) or []


def _resolve(args: dict) -> dict:
	return shift_tools._resolve_shift(args)


def _place_of(row: dict) -> str:
	place = str(row.get("farm_location_gps") or "").strip()
	if not place:
		raise ToolError(
			f"{row['name']} has no farm_location_gps, so there is no place to ask about. The "
			"service asks Open-Meteo what the conditions were HERE, and a shift with no "
			"coordinates gets no timeline — set them on the shift and call again. "
			"'45.52,-122.68' is the cheapest spelling; a place name costs one geocoding lookup "
			"and resolves to the most populous match, which is right for a town and wrong for a "
			"block somebody called Home. Nothing was changed."
		)
	return place


def _describe_reading(row: dict) -> dict:
	return {
		"reading_datetime": str(row.get("reading_datetime") or "") or None,
		"temp_f": row.get("temp_f"),
		"heat_index_f": row.get("heat_index_f"),
		"humidity_pct": row.get("humidity_pct"),
		"wind_speed_mph": row.get("wind_speed_mph"),
		"wind_direction_deg": row.get("wind_direction_deg"),
		"precipitation_mm": row.get("precipitation_mm"),
		"source": row.get("source") or None,
		"fetched_at": str(row.get("fetched_at") or "") or None,
	}


def _extremes(readings: list) -> dict:
	def peak(fieldname):
		values = []
		for row in readings:
			value = row.get(fieldname)
			if value in (None, ""):
				continue
			try:
				values.append(float(value))
			except (TypeError, ValueError):
				continue
		return max(values) if values else None

	return {
		"max_temp_f": peak("temp_f"),
		"max_heat_index_f": peak("heat_index_f"),
		"max_wind_speed_mph": peak("wind_speed_mph"),
	}


# ── 1. fetch_weather_now ────────────────────────────────────────────────────
def fetch_weather_now(args: dict) -> ToolResult:
	"""Append one reading to an open shift right now, ignoring the cache.

	FOR THE MOMENT THE SCHEDULE IS TOO SLOW FOR. The sweep runs every fifteen
	minutes, which is right for documenting a whole day and wrong for a foreman
	watching a crew struggle at eleven who wants the conditions ON THE RECORD
	before they decide anything. The cache is bypassed for the same reason: a
	five-minute-old answer is exactly what they are asking to get past.

	It refuses a closed shift and says which tool to use instead. A closed shift's
	conditions are history and history is what the archive API is for; appending a
	`current` reading to it would file today's weather against last week's crew.
	"""
	_require()
	_require_enabled()
	actor = employee_tool.require_hr_role()
	row = _resolve(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	if not shifts.is_open(row):
		raise ToolError(
			f"{row['name']} ended at {row.get('end_datetime')}. A 'now' reading on a shift that is "
			"over would file today's weather against a crew who went home — the reading would be "
			"true about the place and false about the shift, which is the worst kind of evidence "
			"to put in a compliance record. backfill_weather_for_shift reads the archive for the "
			"hours it actually ran. Nothing was changed."
		)
	place = _place_of(row)

	# BYPASSES THE CACHE, which is the point of the tool. `reset_cache` rather
	# than a per-call flag because the backoff is cached too, and a foreman
	# standing in a hot block asking for a reading is a good enough reason to try
	# the far end once more even if the last attempt was refused.
	weather.reset_cache()
	report = weather.fetch_for_shift(row, use_cache=False)

	if not report["fetched"]:
		raise ToolError(
			f"no reading could be fetched for {row['name']} at {place!r}. {report['reason']} "
			"Nothing was changed, and the scheduled sweep will try again by itself."
		)

	described = shifts.describe(row, with_children=True)
	reading = _describe_reading(report["reading"] or {})
	data = {
		"shift": row["name"],
		"company": row.get("company"),
		"location": row.get("location"),
		"farm_location_gps": place,
		"coordinates": report.get("coordinates"),
		"actor": actor,
		"reading": reading,
		"appended": bool(report["added"]),
		"skipped_as_duplicate": bool(report["skipped"]),
		"weather_reading_count": len(described["weather_timeline"]) + (report["added"] or 0),
		"thresholds_crossed": report["crossed"],
		"threshold_event_logged": report["event_logged"],
		"heat_exposure_event_updated": report.get("heat_event_updated"),
	}
	# v0.140.0. PRESENT ONLY WHEN A HORN WAS ATTEMPTED. See `fetch_for_shift`:
	# the key's absence on a hot shift means this one had already crossed and the
	# crew leader was rung then, which is a different fact from a horn that
	# reached zero handsets and has to stay readable as one.
	if report.get("heat_push") is not None:
		data["heat_push"] = report["heat_push"]
	if not report["added"]:
		data["note"] = (
			f"Open-Meteo answered, and this shift already carries a reading for "
			f"{reading['reading_datetime']}. Nothing was appended: a weather reading is immutable "
			"evidence and a second row for one instant is two answers to one question. The "
			"existing row stands."
		)
	elif report["crossed"]:
		data["note"] = (
			"Reading appended, and it is at or above a threshold. "
			+ (
				"A Threshold Crossed event is now on this shift's timeline. "
				if report["event_logged"]
				else "This shift already carried a Threshold Crossed event at or before this "
				"reading, so no second one was logged — one crossing per shift, or a hot "
				"afternoon buries the water breaks under thirty-six identical rows. "
			)
			+ (
				(
					f"The crew leader's handset was rung: {report['heat_push'].get('sent') or 0} "
					f"device(s) reached"
					+ (f" ({report['heat_push'].get('reason')})" if report["heat_push"].get("reason") else "")
					+ ". "
				)
				if report.get("heat_push") is not None
				else ""
			)
			+ "WHAT THE SHIFT DOES ABOUT IT IS THE FOREMAN'S CALL. Nothing here creates a Heat "
			"Exposure Event: that record says which crew was exposed, what water was provided, "
			"whether the rest cycle was taken and whether anybody showed signs, and it carries a "
			"signature. create_heat_exposure_event is where a person writes it."
		)
	else:
		data["note"] = (
			"Reading appended, and nothing on it reaches this company's thresholds. That is worth "
			"having on the record too — a timeline that exists only on hot days proves nothing "
			"about the days it is missing from, and 'we checked and it was 71 °F' is an answer."
		)
	if report.get("reason"):
		data["warning"] = report["reason"]

	return ToolResult(
		data=data,
		summary=(
			f"fetched weather for {row['name']}: "
			+ (
				f"{reading['temp_f']} °F, heat index {reading['heat_index_f']} °F at "
				f"{reading['reading_datetime']}"
			)
			+ (f"; {len(report['crossed'])} threshold(s) crossed" if report["crossed"] else "")
		),
		docstatus_delta="0 → 0 (amended)",
	)


# ── 2. backfill_weather_for_shift ───────────────────────────────────────────
def backfill_weather_for_shift(args: dict) -> ToolResult:
	"""Reconstruct a closed shift's timeline from Open-Meteo's historical archive.

	THE SHIFTS THAT RAN BEFORE THE SERVICE WAS SWITCHED ON ARE THE ONES THIS IS
	FOR, and there is no version of the operation where they do not exist: every
	site that installs v0.19.4 has a season of shifts behind it with an empty
	weather table. A shift with no timeline is not a shift that was compliant or
	non-compliant — it is a shift nobody can say anything about, and Open-Meteo
	still knows what the weather was that day.

	IT REFUSES AN OPEN SHIFT. The archive is a reanalysis of what already
	happened; a shift still running has hours in it that have not happened yet,
	and the sweep is already documenting the ones that have.

	IT IS IDEMPOTENT AND SAYS SO IN NUMBERS. Every reading is matched against the
	minute already on the timeline, so a second run appends nothing and reports
	what it skipped. A reading is never edited — running this over a shift that
	was ALSO swept live keeps the live readings and fills the gaps between them.

	IT LOGS NO COMPLIANCE EVENTS, AND THAT IS THE ONE JUDGEMENT CALL IN THIS TOOL.
	A backfilled crossing is a true statement about the weather and a false one
	about the shift: writing a Threshold Crossed event dated last July onto a
	closed, signed record would put an observation on the timeline that nobody
	made at the time, next to water breaks that somebody did. So the crossings are
	COUNTED and REPORTED here, for a human to read and decide about — which is
	also what makes the numbers usable: 'four of these readings were over 80 °F'
	is the sentence that tells a foreman whether this shift needed a heat record.
	"""
	_require()
	_require_enabled()
	actor = employee_tool.require_hr_role()
	row = _resolve(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	end = str(row.get("end_datetime") or "").strip()
	if not end:
		raise ToolError(
			f"{row['name']} is still open. The archive API is a reanalysis of hours that have "
			"already happened, and an open shift has hours in it that have not — backfilling one "
			"would write a partial timeline that looks complete. The scheduled sweep is already "
			"documenting this shift every fifteen minutes, and fetch_weather_now adds a reading "
			"immediately. Nothing was changed."
		)
	start = str(row.get("start_datetime") or "").strip()
	if not start:
		raise ToolError(
			f"{row['name']} has no start_datetime, so there is no period to fetch. Nothing was changed."
		)
	place = _place_of(row)

	coordinates = weather.resolve_location(place)
	if not coordinates:
		raise ToolError(
			f"{place!r} on {row['name']} could not be resolved to coordinates, so there is nowhere "
			"to ask about. A latitude,longitude pair always works and costs no lookup; a place "
			"name needs Open-Meteo's geocoder, which may be unreachable or may simply not know "
			"this name. Nothing was changed."
		)
	lat, lon = coordinates
	readings = weather.fetch_archive(lat, lon, start, end)
	if readings is None:
		waiting = weather.backoff_seconds_remaining(f"{round(lat, 4)},{round(lon, 4)}")
		raise ToolError(
			f"the archive API returned nothing usable for {place!r}"
			+ (f" and is backed off for another {waiting}s" if waiting else "")
			+ ". Note the archive is a DIFFERENT dataset from the forecast API and lags real time "
			"by a day or two, so a shift that ended this morning may simply not be in it yet. "
			"Nothing was changed."
		)

	# THE ARCHIVE ANSWERS BY WHOLE DAYS and this shift is a span of hours inside
	# them. Filtering to the shift's own period is what stops a six-hour morning
	# shift acquiring a timeline that runs to midnight — which would be a
	# defensible weather record and a false statement about an exposure period.
	within = [
		reading
		for reading in readings
		if start[:19] <= str(reading.get("reading_datetime") or "")[:19] <= end[:19]
	]
	outside = len(readings) - len(within)

	report = weather.append_readings(row["name"], within)
	limits = weather.thresholds_for(str(row.get("company") or ""))
	crossings = [reading for reading in within if weather._heat_crossing(reading, limits)]

	timeline = shifts.weather_of(row["name"])
	data = {
		"shift": row["name"],
		"company": row.get("company"),
		"period": {"from": start, "to": end},
		"farm_location_gps": place,
		"coordinates": {"latitude": lat, "longitude": lon},
		"actor": actor,
		"added": report["added"],
		"skipped_as_duplicate": report["skipped"],
		"failed": report["failed"],
		"returned_by_the_archive": len(readings),
		"outside_the_shift_period": outside,
		"weather_reading_count": len(timeline),
		"thresholds": limits,
		"readings_at_or_above_the_heat_threshold": len(crossings),
		"extremes": _extremes(timeline),
		"source": weather.SOURCE_ARCHIVE,
	}
	if report.get("note"):
		data["warning"] = report["note"]
	if not report["added"] and report["skipped"]:
		data["note"] = (
			f"Nothing was added: all {report['skipped']} archive reading(s) for this period are "
			"already on the timeline. That is what a second run looks like, and it is the "
			"guarantee — a reading is immutable evidence and is only ever appended."
		)
	else:
		data["note"] = (
			f"{report['added']} reading(s) appended from the archive, at its own HOURLY "
			"granularity — a live-swept shift carries one every fifteen minutes and this carries "
			"one an hour. Every row says so in its `source` column, so nobody reads a "
			"reconstruction as something contemporaneous."
		)
	if crossings:
		data["threshold_note"] = (
			f"{len(crossings)} of these reading(s) are at or above this company's heat threshold "
			f"({limits['heat_threshold_temp_f']:.0f} °F air / "
			f"{limits['heat_threshold_heat_index_f']:.0f} °F heat index), the earliest at "
			f"{min(str(entry.get('reading_datetime')) for entry in crossings)}. NO COMPLIANCE "
			"EVENT WAS WRITTEN. A Threshold Crossed row dated last July on a closed and signed "
			"shift would be an observation nobody made, sitting next to water breaks somebody "
			"did. If this shift needed a Heat Exposure Event and has none, "
			"create_heat_exposure_event files it — and its maxima now compute from this timeline."
		)
	return ToolResult(
		data=data,
		summary=(
			f"backfilled {row['name']} from the archive: {report['added']} added, "
			f"{report['skipped']} already present, {report['failed']} failed"
		),
		docstatus_delta="0 → 0 (amended)",
	)


# ── 3. list_shifts_missing_weather ──────────────────────────────────────────
def list_shifts_missing_weather(args: dict) -> ToolResult:
	"""Closed shifts whose weather timeline is thinner than their own length. Read-only.

	THE WORKLIST FOR THE BACKFILL, which is the difference between a tool somebody
	uses once and one they can finish a season with.

	THE TEST IS ONE READING PER HOUR OF SHIFT, which is the archive's own
	granularity — so a fully backfilled shift never appears here, and a live-swept
	one clears the bar four times over. It is a HEURISTIC and says so: a shift
	swept live for its first two hours and then missed has readings and is still
	missing most of its timeline, and this is what finds it. A shift with no
	coordinates is reported separately, because no amount of backfilling will fix
	one and the fix is a different action entirely.
	"""
	_require()
	actor = employee_tool.require_hr_role()
	limit = min(as_limit(args), RECORD_CAP)

	filters = {"end_datetime": ("is", "set")}
	company = resolve_company(as_str(args, "company"), required=False)
	if company:
		employee_tool.require_company_scope(actor, company)
		filters["company"] = company
	else:
		allowed = _readable_companies(actor)
		if allowed:
			filters["company"] = ("in", allowed)

	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters["start_datetime"] = ("between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"])
	elif from_date:
		filters["start_datetime"] = (">=", f"{from_date} 00:00:00")
	elif to_date:
		filters["start_datetime"] = ("<=", f"{to_date} 23:59:59")

	found = shifts.rows(filters, limit=max(limit * 2, limit))
	thin, no_place, complete = [], [], 0
	for row in found:
		name = str(row.get("name") or "")
		hours = shifts.hours_between(str(row.get("start_datetime") or ""), str(row.get("end_datetime") or ""))
		readings = shifts.weather_of(name)
		expected = max(1, round((hours or 0) * READINGS_PER_HOUR))
		entry = {
			"name": name,
			"company": row.get("company"),
			"foreman_name": row.get("foreman_name"),
			"shift_type": row.get("shift_type"),
			"location": row.get("location"),
			"farm_location_gps": row.get("farm_location_gps") or None,
			"start_datetime": str(row.get("start_datetime") or "") or None,
			"end_datetime": str(row.get("end_datetime") or "") or None,
			"shift_hours": hours,
			"weather_reading_count": len(readings),
			"readings_expected": expected,
		}
		if not str(row.get("farm_location_gps") or "").strip():
			no_place.append(entry)
			continue
		if len(readings) < expected:
			thin.append(entry)
		else:
			complete += 1

	truncated = len(thin) > limit
	thin = thin[:limit]
	data = {
		"company": company,
		"count": len(thin),
		"limit": limit,
		"truncated": truncated,
		"shifts": thin,
		"without_coordinates": no_place,
		"already_documented": complete,
		"heuristic": (
			f"A closed shift is reported when it carries fewer than {READINGS_PER_HOUR:.0f} "
			"reading(s) per hour of its own length — the archive's granularity, so a fully "
			"backfilled shift never appears here and a live-swept one clears it four times over."
		),
		"note": (
			f"{len(thin)} closed shift(s) can be documented from the archive. "
			"backfill_weather_for_shift takes one docname at a time, deliberately: it makes an "
			"outbound request per shift, and a tool that walked a season in one call would be a "
			"tool that rate-limited this site in one call."
			if thin
			else "Every closed shift in this selection with coordinates carries a timeline as "
			"dense as its own length. Nothing to backfill."
		),
	}
	if no_place:
		data["coordinates_note"] = (
			f"{len(no_place)} closed shift(s) have NO farm_location_gps, so no amount of "
			"backfilling will document them — there is no place to ask about. They are listed "
			"separately because the fix is a different action: put the coordinates on the shift "
			"first, then backfill. It is also worth fixing on the shifts still to come, since "
			"the same blank is why they will get no live timeline either."
		)
	if truncated:
		data["truncation_note"] = (
			f"More than {limit} shift(s) matched and this is the first {limit}. Narrow by company "
			"or period before relying on the counts above."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{len(thin)} closed shift(s) missing weather"
			+ (f" for {company}" if company else "")
			+ (f"; {len(no_place)} with no coordinates at all" if no_place else "")
		),
	)


# ── 4. get_weather_timeline ─────────────────────────────────────────────────
def get_weather_timeline(args: dict) -> ToolResult:
	"""One shift's conditions across its exposure period, optionally windowed. Read-only.

	THE ANSWER TO 'HOW HOT WAS IT WHEN THE BREAK WAS CALLED', without reading the
	whole shift. `get_shift` returns the timeline alongside the crew, the events
	and the heat record, which is right for an audit and too much for a question
	about one hour — so this returns the readings, the extremes, and the times the
	shift was at or above its own company's thresholds.

	IT SAYS WHERE EACH READING CAME FROM. A timeline of live fifteen-minute
	readings and a timeline reconstructed from the hourly archive are different
	kinds of evidence, and the `source` column on every row plus the summary here
	are what stop the second being read as the first.
	"""
	_require()
	actor = employee_tool.require_hr_role()
	row = _resolve(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	readings = shifts.weather_of(row["name"])
	from_dt = as_str(args, "from_datetime")
	to_dt = as_str(args, "to_datetime")
	if from_dt:
		readings = [entry for entry in readings if str(entry.get("reading_datetime") or "") >= from_dt]
	if to_dt:
		readings = [entry for entry in readings if str(entry.get("reading_datetime") or "") <= to_dt]
	truncated = len(readings) > TIMELINE_CAP
	readings = readings[:TIMELINE_CAP]

	limits = weather.thresholds_for(str(row.get("company") or ""))
	crossings = [
		str(entry.get("reading_datetime") or "")
		for entry in readings
		if weather._heat_crossing(entry, limits)
	]
	sources = sorted({str(entry.get("source") or "unrecorded") for entry in readings})

	data = {
		"shift": row["name"],
		"company": row.get("company"),
		"shift_type": row.get("shift_type"),
		"location": row.get("location"),
		"farm_location_gps": row.get("farm_location_gps") or None,
		"start_datetime": str(row.get("start_datetime") or "") or None,
		"end_datetime": str(row.get("end_datetime") or "") or None,
		"open": shifts.is_open(row),
		"window": {"from": from_dt or None, "to": to_dt or None},
		"count": len(readings),
		"truncated": truncated,
		"readings": [_describe_reading(entry) for entry in readings],
		"extremes": _extremes(readings),
		"thresholds": limits,
		"readings_at_or_above_the_heat_threshold": len(crossings),
		"first_crossing": crossings[0] if crossings else None,
		"sources": sources,
	}
	if not readings:
		data["note"] = (
			"NO READINGS IN THIS SELECTION. If the shift has no farm_location_gps there is "
			"nothing to fetch and never will be; if it is closed, backfill_weather_for_shift "
			"reads the archive for the hours it ran; if it is open and this is empty, check that "
			"`enabled` is ticked on Weather Settings and that the bench's scheduler is running — "
			"a bench with its scheduler off collects no timeline at all and says nothing about it."
		)
	elif crossings:
		data["note"] = (
			f"This shift was at or above its heat threshold from {crossings[0]}, across "
			f"{len(crossings)} of {len(readings)} reading(s). That is the exposure period "
			f"{shifts.CITATION}'s obligations run from — water at the required rate, shade within "
			"reach, the preventative cool-down rest cycle, observation for signs — and the "
			"shift's own compliance events are what say whether they happened."
		)
	else:
		data["note"] = (
			"Nothing on this timeline reaches this company's heat threshold. Worth having on the "
			"record: a timeline that exists only on hot days proves nothing about the days it is "
			"missing from."
		)
	if weather.SOURCE_ARCHIVE in sources and weather.SOURCE_CURRENT in sources:
		data["source_note"] = (
			"This timeline is MIXED — some readings were fetched while the shift was running and "
			"some were reconstructed from the archive afterwards. Both are true; they are not "
			"equally strong, and the `source` on each row is what tells them apart."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{len(readings)} reading(s) on {row['name']}"
			+ (
				f"; peak {data['extremes']['max_temp_f']} °F / heat index "
				f"{data['extremes']['max_heat_index_f']} °F"
				if readings
				else ""
			)
			+ (f"; {len(crossings)} at or above threshold" if crossings else "")
		),
	)


# ── 5. get_weather_settings ─────────────────────────────────────────────────
def get_weather_settings(args: dict) -> ToolResult:
	"""What this site fetches, how often, and the numbers a reading is read against.

	Read-only, AND THERE IS NO WRITE COUNTERPART. The three URLs here are outbound
	endpoints and the three thresholds decide whether a hot afternoon gets logged
	at all; a tool that could change either would be one sentence away from
	pointing this site's weather somewhere else, or from raising the heat
	threshold past anything Oregon produces so that no shift ever crosses it. The
	Desk form is the write surface, where a person types the number and Frappe's
	version trail records who did.

	IT REPORTS THE ACTIVE OVERRIDES BY COMPANY, because "the threshold is 80" is
	false on a site where one entity set 75 and is the answer somebody would
	otherwise carry into a conversation about a citation.
	"""
	_require_enabled_read()
	actor = employee_tool.require_hr_role()

	allowed = _readable_companies(actor)
	overrides = []
	for row in weather.override_rows():
		# SCOPED LIKE EVERY OTHER READ. A threshold row names a company, and a
		# principal restricted to one entity has no business reading another
		# entity's compliance configuration off a settings page.
		if allowed and str(row.get("company") or "") not in allowed:
			continue
		overrides.append(
			{
				"company": row.get("company"),
				"heat_threshold_temp_f": row.get("heat_threshold_temp_f"),
				"heat_threshold_heat_index_f": row.get("heat_threshold_heat_index_f"),
				"wind_threshold_mph_spray_block": row.get("wind_threshold_mph_spray_block"),
			}
		)

	open_now = [
		entry["name"]
		for entry in weather.open_shifts()
		if not allowed or str(entry.get("company") or "") in allowed
	]
	data = {
		"enabled": weather.enabled(),
		"fetch_interval_minutes": weather.fetch_interval_minutes(),
		"cache_ttl_seconds": weather.cache_ttl_seconds(),
		"http_timeout_seconds": weather.http_timeout_seconds(),
		"defaults": weather.thresholds_for(""),
		"per_company_overrides": overrides,
		"endpoints": {
			"current": weather.base_url("current"),
			"archive": weather.base_url("archive"),
			"geocoding": weather.base_url("geocoding"),
		},
		"schedule": "*/15 * * * *",
		"open_shifts_with_coordinates": open_now,
		"actor": actor,
		"cadence_note": (
			"THE CRON IS THE CEILING AND fetch_interval_minutes IS THE FLOOR. A Frappe cron "
			"expression is a static string in hooks.py and cannot be rewritten from a form, so "
			"the sweep runs every fifteen minutes on every site — and a shift whose newest "
			f"reading is younger than {weather.fetch_interval_minutes()} minute(s) is skipped. "
			"Raising the setting gets readings less often, which is the change operations "
			"actually ask for; lowering it below fifteen changes nothing."
		),
		"write_note": (
			"There is no update_weather_settings tool and there will not be one. These are three "
			"outbound URLs and three thresholds that decide whether a hot afternoon is logged at "
			"all — a model that could raise the heat threshold to 200 °F would produce a site "
			"that behaves normally and never says anything is wrong. An operator edits Weather "
			"Settings in the Desk."
		),
	}
	if not data["enabled"]:
		data["note"] = (
			"WEATHER IS SWITCHED OFF on this site. The scheduled sweep returns without a query, "
			"the two mutating tools refuse, and no shift is collecting a timeline right now — "
			"which is a gap that cannot be recovered for an open shift, because `current` only "
			"answers about now. Closed shifts can still be reconstructed from the archive later."
		)
	elif not open_now:
		data["note"] = (
			"Weather is on and no shift is currently open with coordinates on it, so the sweep "
			"has nothing to walk. That is the ordinary state outside working hours."
		)
	else:
		data["note"] = (
			f"{len(open_now)} open shift(s) with coordinates are being documented every fifteen minutes."
		)
	return ToolResult(
		data=data,
		summary=(
			("weather is ON" if data["enabled"] else "weather is OFF")
			+ f"; heat threshold {data['defaults']['heat_threshold_temp_f']:.0f} °F air / "
			f"{data['defaults']['heat_threshold_heat_index_f']:.0f} °F heat index"
			+ (f"; {len(overrides)} company override(s)" if overrides else "")
			+ f"; {len(open_now)} open shift(s) being swept"
		),
	)


def _require_enabled_read() -> None:
	"""The doctype has to exist to be read; the switch does NOT have to be on.

	`get_weather_settings` is the tool somebody calls to find out WHY nothing is
	being fetched, and a read that refused because the thing it reports is off
	would refuse in exactly the moment it is useful.
	"""
	compat.require_doctype(
		weather.SETTINGS_DOCTYPE,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)

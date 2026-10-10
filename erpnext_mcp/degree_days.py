# SPDX-License-Identifier: MIT
"""Growing degree days per block, and the crop stage they suggest. v0.249.0. Approved queue item 10;
decision 12 ("estimate the stage from calendar + degree days"); docs/design/ccf_core_work_timing.md §5.

* ACCUMULATION is weather, not judgement: daily highs and lows for the block's H3 cell (resolution 7) from
  the biofix (default 1 January, a setting) — Open-Meteo's archive up to a week ago, its forecast API for the
  last week and the next 16 days. Degree days per day = (min(high, cutoff) + max(low, base)) / 2 − base,
  never below zero (the simple average with a horizontal upper cutoff). v0.297.0: the base is per crop in the
  `crop_phenology` IPM setting — sweet cherry 40.1 °F (4.5 °C, ISHS 1020_29), 50 °F for crops with no row;
  cutoff 86 °F.
* THE STAGE ESTIMATE is only as good as the table behind it, so there is NO built-in table: ERPNext MCP
  Settings → Degree-Day Stage Table, one line per crop and stage ("Sweet Cherry: 51 = 120"), drafted from the
  Reference Library's OSU/WSU guides. Without a line for the crop, there is no estimate and it says so.
* AN ESTIMATE NEVER PASSES A STAGE CHECK (design §5): it is shown beside "Go — verify stage" and offered as the
  stage to look for. Only an observed stage (`phenology.bbch`) passes.
"""

from __future__ import annotations

import datetime
import time

import frappe

from . import ccf_providers, compat, settings

DEFAULT_BASE_F = 50.0
DEFAULT_CUTOFF_F = 86.0
TTL_SECONDS = 6 * 3600
_CACHE: dict = {}


def _today() -> datetime.date:
	return datetime.date.fromisoformat(str(frappe.utils.today())[:10])


def biofix(year: int, crop_biofix: str | None = None) -> datetime.date:
	"""The day the count starts: the crop's own (crop_phenology, v0.303.0), else the site's Degree-Day Biofix."""
	raw = str(crop_biofix or settings.get_settings().get("degree_day_biofix") or "01-01").strip()
	try:
		month, day = (int(x) for x in raw.split("-")[:2])
		return datetime.date(year, month, day)
	except ValueError:
		return datetime.date(year, 1, 1)


def parse_table(raw: str) -> tuple[dict, list]:
	"""{crop: {base, cutoff, stages: [(gdd, bbch)], explicit}}, problems. Lines: 'Crop: 51 = 120', 'Crop: base = 50'."""
	table: dict = {}
	problems = []
	for number, line in enumerate(str(raw or "").splitlines(), 1):
		line = line.strip()
		if not line or line.startswith("#"):
			continue
		crop, _, rest = line.partition(":")
		left, _, right = rest.partition("=")
		crop, left, right = crop.strip(), left.strip().lower(), right.strip()
		try:
			value = float(right)
		except ValueError:
			problems.append(f"line {number}: write 'Crop: 51 = 120' (BBCH = degree days) or 'Crop: base = 50'.")
			continue
		entry = table.setdefault(crop.lower(), {"crop": crop, "base": DEFAULT_BASE_F, "cutoff": DEFAULT_CUTOFF_F, "stages": []})
		if left in ("base", "cutoff"):
			entry[left] = value
			entry.setdefault("explicit", set()).add(left)
		elif left.isdigit() and len(left) <= 2:
			entry["stages"].append((value, left.zfill(2)))
		else:
			problems.append(f"line {number}: {left!r} is not a BBCH code, 'base' or 'cutoff'.")
	for entry in table.values():
		entry["stages"].sort()
	return table, problems


def phenology_row(crop: str) -> tuple:
	"""v0.297.0. (row, version): the crop's phenology base from the `crop_phenology` IPM setting — matched by name or
	alias — else its `default` row (50 °F)."""
	from . import pest_dd

	body, version = pest_dd.phenology()
	wanted = str(crop or "").strip().lower()
	for key, row in (body.get("crops") or {}).items():
		names = {str(key).lower(), str(row.get("crop") or "").lower(), *[str(a).lower() for a in row.get("aliases") or []]}
		if wanted and wanted in names:
			return row, version
	return dict(body.get("default") or {"base_f": DEFAULT_BASE_F, "cutoff_f": DEFAULT_CUTOFF_F}), version


def method_for(row: dict, variety: str = "", block: str = "") -> tuple:
	"""v0.310.0. (name, method) for a block: `by_block`, else the variety's `by_variety`, else `default_method`.
	({}, "") where the row has no methods."""
	methods = row.get("methods") or {}
	if not methods:
		return "", {}
	by_variety = {str(k).casefold(): v for k, v in (row.get("by_variety") or {}).items()}
	name = ((row.get("by_block") or {}).get(block) or by_variety.get(str(variety or "").casefold())
	        or row.get("default_method") or next(iter(methods)))
	return name, dict(methods.get(name) or {})


def crop_entry(crop: str, variety: str = "", block: str = "") -> dict:
	"""The crop's base, cutoff and stage table. v0.297.0: the BASE comes from the `crop_phenology` setting (per crop,
	config not code — sweet cherry 40.1 °F); the Degree-Day Stage Table supplies the stages, and its `base =` line
	only for a crop the setting has no row for."""
	table, _ = parse_table(settings.get_settings().get("degree_day_stage_table") or "")
	entry = dict(table.get(str(crop or "").lower()) or {"crop": crop, "base": DEFAULT_BASE_F, "cutoff": DEFAULT_CUTOFF_F,
	                                                     "stages": []})
	explicit = entry.pop("explicit", set())
	row, version = phenology_row(crop)
	has_row = row.get("crop") is not None
	if has_row or "base" not in explicit:
		entry["base"] = float(row.get("base_f", DEFAULT_BASE_F))
		entry["base_source"] = f"crop_phenology {version}: {row.get('citation') or ''}".strip()
		if has_row and "base" in explicit:
			entry["base_note"] = (f"The Degree-Day Stage Table's base line for {entry.get('crop')} is not used — the "
			                      "crop_phenology setting sets this crop's base.")
	else:
		entry["base_source"] = "Degree-Day Stage Table (ERPNext MCP Settings)"
	if "cutoff" not in explicit and row.get("cutoff_f") not in (None, ""):
		entry["cutoff"] = float(row["cutoff_f"])
	entry["alternates"] = row.get("alternates") or []
	# v0.303.0. The crop's stage table from crop_phenology wins over the Settings text table where it has rows.
	stages = []
	for stage in row.get("stages") or []:
		bbch, dd = (stage.get("bbch"), stage.get("dd")) if isinstance(stage, dict) else stage
		stages.append((float(dd), str(bbch).zfill(2)))
	if stages:
		entry["stages"] = sorted(stages)
		entry["stages_source"] = f"crop_phenology {version}"
	elif entry.get("stages"):
		entry["stages_source"] = "Degree-Day Stage Table (ERPNext MCP Settings)"
	entry["biofix"] = row.get("biofix") or None
	# v0.310.0. The method for this block / variety sets the base, the biofix and the stages, and is reported.
	name, method = method_for(row, variety, block)
	if method:
		entry["base"] = float(method.get("base_f", entry["base"]))
		entry["biofix"] = method.get("biofix") or entry["biofix"]
		entry["base_source"] = f"crop_phenology {version}, method {name}: {method.get('citation') or ''}".strip()
		by_variety = {str(k).casefold(): v for k, v in (method.get("stages_by_variety") or {}).items()}
		own = by_variety.get(str(variety or "").casefold())
		if own:
			entry["stages"] = sorted((float(dd), str(bbch).zfill(2)) for bbch, dd in own)
			entry["stages_source"] = f"crop_phenology {version}, method {name}, variety {variety}"
		chill = None
		if method.get("biofix") == "chill":
			hours = (row.get("chill_hours_by_variety") or {}).get(variety) or method.get("chill_hours_required")
			chill = {"start": method.get("chill_start") or "11-01", "hours_required": float(hours or 0),
			         "model": method.get("chill_model")}
		entry["method"] = {"name": name, "biofix": method.get("biofix"), "base_f": float(method.get("base_f")),
		                   "base_c": method.get("base_c"), "citation": method.get("citation"),
		                   "chosen_by": ("block" if (row.get("by_block") or {}).get(block) else
		                                 "variety" if str(variety or "").casefold() in
		                                 {str(k).casefold() for k in (row.get("by_variety") or {})} else "default"),
		                   **({"chill": chill} if chill else {})}
	return entry


def chill_hours(high, low, lower: float = 32.0, upper: float = 45.0) -> float:
	"""v0.310.0. Hours of one day between `lower` and `upper` °F, estimated from its high and low (a linear day)."""
	if high is None or low is None:
		return 0.0
	high, low = float(high), float(low)
	if high <= low:
		return 24.0 if lower < high <= upper else 0.0
	inside = max(0.0, min(high, upper) - max(low, lower))
	return 24.0 * inside / (high - low)


def chill_biofix(temps: dict, start: datetime.date, required: float) -> tuple:
	"""v0.310.0. (the day the chill requirement was met, hours counted) — (None, hours) while it is not yet met."""
	total = 0.0
	for day in sorted(temps):
		date = datetime.date.fromisoformat(day)
		if date < start:
			continue
		high, low = temps[day]
		total += chill_hours(high, low)
		if total >= required:
			return date + datetime.timedelta(days=1), round(total, 1)
	return None, round(total, 1)


def gdd(high, low, base: float, cutoff: float) -> float:
	if high is None or low is None:
		return 0.0
	high = min(float(high), cutoff)
	low = max(float(low), base)
	return max(0.0, (high + low) / 2 - base)


#: v0.264.0. {H3 cell: metres} — the weather grid cell's elevation, as Open-Meteo last stated it.
LAST_ELEVATION: dict = {}


def daily_temps(lat: float, lon: float, start: datetime.date, end: datetime.date) -> dict:
	"""{date: (high, low)} — archive to a week ago, forecast API for the rest. Cached per cell."""
	from .services import weather

	cell = ccf_providers._cell(lat, lon)
	key = (cell, start.isoformat(), end.isoformat())
	hit = _CACHE.get(key)
	if hit and hit[0] > time.time():
		return hit[1]
	out: dict = {}
	split = min(end, _today() - datetime.timedelta(days=6))
	if start <= split:
		payload = weather._get_json(
			weather.base_url("archive"),
			{"latitude": lat, "longitude": lon, "start_date": start.isoformat(), "end_date": split.isoformat(),
			 "daily": "temperature_2m_max,temperature_2m_min", "timezone": "auto", **weather.UNITS},
			f"degree-days:{cell}",
		) or {}
		_merge(out, payload)
	payload = weather._get_json(
		weather.base_url("current"),
		{"latitude": lat, "longitude": lon, "daily": "temperature_2m_max,temperature_2m_min", "past_days": 7,
		 "forecast_days": 16, "timezone": "auto", **weather.UNITS},
		f"degree-days:{cell}",
	) or {}
	_merge(out, payload)
	# v0.276.1. Only the days asked for. The forecast call always answers around TODAY, so for a past `as_of` it
	# added October days to a July series — a projection could have read them.
	out = {day: value for day, value in out.items() if start.isoformat() <= day <= end.isoformat()}
	# v0.264.0. The grid cell's own elevation, which Open-Meteo states with every answer: the pest DD
	# lapse-rate offset compares the block's ground with it.
	if isinstance(payload, dict) and payload.get("elevation") is not None:
		LAST_ELEVATION[cell] = payload["elevation"]
	_CACHE[key] = (time.time() + TTL_SECONDS, out)
	return out


def _merge(out: dict, payload: dict) -> None:
	daily = (payload or {}).get("daily") or {}
	days = daily.get("time") or []
	highs = daily.get("temperature_2m_max") or [None] * len(days)
	lows = daily.get("temperature_2m_min") or [None] * len(days)
	for index, day in enumerate(days):
		out[str(day)] = (highs[index], lows[index])


def accumulate(temps: dict, start: datetime.date, today: datetime.date, base: float, cutoff: float) -> dict:
	season, ahead, series, missing = 0.0, [], [], 0
	for day in sorted(temps):
		date = datetime.date.fromisoformat(day)
		if date < start:
			continue
		high, low = temps[day]
		value = gdd(high, low, base, cutoff)
		if high is None or low is None:
			missing += 1
		if date <= today:
			season += value
		else:
			ahead.append({"date": day, "gdd": round(value, 1)})
		series.append({"date": day, "gdd": round(value, 1)})
	return {"season": round(season, 1), "ahead": ahead, "series": series, "missing_days": missing}


def estimate(entry: dict, season: float, ahead: list) -> dict:
	stages = entry.get("stages") or []
	if not stages:
		return {"estimated_bbch": None, "note": f"No degree-day stage table for {entry.get('crop') or 'this crop'} — "
		                                        "add its stages to the crop_phenology config (config action=draft "
		                                        "kind=crop_phenology), or lines in ERPNext MCP Settings → Degree-Day "
		                                        "Stage Table."}
	reached = [code for need, code in stages if season >= need]
	current = reached[-1] if reached else None
	upcoming = next(((need, code) for need, code in stages if season < need), None)
	when = None
	if upcoming:
		running = season
		for day in ahead:
			running += day["gdd"]
			if running >= upcoming[0]:
				when = day["date"]
				break
	return {
		"estimated_bbch": current,
		"next_bbch": upcoming[1] if upcoming else None,
		"next_at_gdd": upcoming[0] if upcoming else None,
		"next_expected": when,
		"note": "An estimate from degree days. Only an observed stage passes a stage check.",
	}


def for_block(block: str, crop: str = "", as_of: str = "") -> dict:
	point = ccf_providers.block_point({"location_doctype": "Field", "location": block}, {"doctype": "Field"})
	if not point:
		return {"block": block, "available": False, "note": "the block has no boundary or centroid to look up weather for."}
	if not ccf_providers.forecast_enabled():
		return {"block": block, "available": False, "note": "block forecasts are off (Weather Settings)."}
	today = datetime.date.fromisoformat(str(as_of)[:10]) if as_of else _today()
	crop = crop or _crop_of(block)
	variety = _variety_of(block)
	entry = crop_entry(crop, variety, block)
	chill = (entry.get("method") or {}).get("chill")
	if chill:
		# v0.310.0. After chill satisfaction: count chill from the autumn before, then degree days from the day after.
		season_year = today.year if today >= datetime.date(today.year, 9, 1) else today.year - 1
		month, day = (int(x) for x in chill["start"].split("-")[:2])
		chill_from = datetime.date(season_year if month >= 7 else season_year + 1, month, day)
		chill_temps = daily_temps(point[0], point[1], chill_from, today)
		met, counted = chill_biofix(chill_temps, chill_from, chill["hours_required"])
		chill.update({"counted_from": chill_from.isoformat(), "hours_counted": counted,
		              "met_on": met.isoformat() if met else None})
		if not met:
			return {"block": block, "available": True, "crop": crop or None, "variety": variety or None,
			        "method": entry["method"], "biofix": None, "gdd_season": 0.0, "estimated_bbch": None,
			        "note": f"chill requirement not yet met ({counted:g} of {chill['hours_required']:g} h) — the "
			                "degree-day count has not started.", "series": []}
		start = met
	else:
		start = biofix(today.year, entry.get("biofix"))
	temps = daily_temps(point[0], point[1], start, today + datetime.timedelta(days=16))
	acc = accumulate(temps, start, today, entry["base"], entry["cutoff"])
	return {
		"block": block,
		"available": True,
		"crop": crop or None,
		"variety": variety or None,
		# v0.310.0. The method each answer was computed with — biofix rule, base, citation, what chose it.
		**({"method": entry["method"]} if entry.get("method") else {}),
		"biofix": start.isoformat(),
		"base_f": entry["base"],
		"base_source": entry.get("base_source"),
		**({"base_note": entry["base_note"]} if entry.get("base_note") else {}),
		"base_alternates": entry.get("alternates") or [],
		"stages_source": entry.get("stages_source"),
		"cutoff_f": entry["cutoff"],
		"gdd_season": acc["season"],
		"gdd_next_7": round(sum(d["gdd"] for d in acc["ahead"][:7]), 1),
		"missing_days": acc["missing_days"],
		**estimate(entry, acc["season"], acc["ahead"]),
		"series": acc["series"],
	}


def _variety_of(block: str) -> str:
	"""v0.310.0. The block's (primary) variety, for the per-variety phenology method."""
	if compat.has_field("Field", "variety"):
		return str(frappe.db.get_value("Field", block, "variety") or "")
	return ""


def _crop_of(block: str) -> str:
	for field in ("crop", "primary_crop"):
		if compat.has_field("Field", field):
			value = frappe.db.get_value("Field", block, field)
			if value:
				return str(value)
	return ""


# ── the CCF provider ────────────────────────────────────────────────────────
def _values(subject: dict, ctx: dict) -> dict:
	block = ""
	if str(subject.get("location_doctype") or "") == "Field":
		block = str(subject.get("location") or "")
	elif subject.get("block"):
		block = str(subject["block"])
	if not block:
		return {}
	data = for_block(block, as_of=str(ctx.get("as_of_date") or ""))
	if not data.get("available"):
		return {}
	return {k: data.get(k) for k in ("gdd_season", "gdd_next_7", "estimated_bbch", "next_bbch", "next_expected")}


def register(ccf) -> None:
	p = ccf._p
	ccf.register(
		ccf.Provider(
			"degree_days",
			{
				"gdd_season": p("number", "Growing degree days since the biofix, to the day judged.", "°F·day", 180),
				"gdd_next_7": p("number", "Degree days forecast for the next 7 days.", "°F·day", 40),
				"estimated_bbch": p("bbch", "The stage the degree-day table suggests (an estimate; never passes a stage check).", example="53"),
				"next_bbch": p("bbch", "The next stage in the table.", example="55"),
				"next_expected": p("date", "When the forecast reaches it.", example="2026-04-02"),
			},
			lambda subject, ctx: _values(subject, ctx),
			past=False,
			description="Growing degree days for the block (Open-Meteo archive + forecast), and the stage table's estimate.",
		)
	)

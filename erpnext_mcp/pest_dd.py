# SPDX-License-Identifier: MIT
"""Pest degree-day models on the farm's own weather, per block. v0.264.0 (Tim, 2026-10-06).

THE WEATHER IS THE FARM'S: Open-Meteo, the source Weather Settings configures and `degree_days` already
reads (archive to a week ago, forecast after) at the block's H3 cell. Tim calls it "the metro weather".
NO LITERATURE DATES: a model's biofix is a rule (1 January, 1 March, the block's first trap catch) and
its events are degree-day totals; every date this module states is where the block's own accumulated
and forecast degree days cross a total.

TWO PIECES OF CONFIG, BOTH DATA, both versioned in Farm Config Version (kind "IPM Setting"):

* `pest_dd_models` — per pest: base and upper threshold (°F), biofix rule, events (name, °F·day total),
  citation, and whether the numbers still need verifying. Seeded from `ipm_reference` where it has a
  model (4 of 28), with the units made explicit; every other pest has no DD model until one is added.
* `pest_dd_offsets` — the per-block temperature offset, °F added to each day's high and low: a
  conservative aspect table scaled by slope, an elevation lapse rate, and per-block calibrated values.
  Calibration (`suggest_calibration`) compares what was logged on a block with the model and writes a
  DRAFT version for a person to publish. Nothing is ever auto-applied.

EVERY ANSWER CARRIES ITS PROVENANCE: the weather source and grid cell, the offset applied and why, the
config versions in force, and the model's citation.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime
import math
import statistics

import frappe

from . import compat

KIND = "IPM Setting"
MODELS_KEY = "pest_dd_models"
OFFSETS_KEY = "pest_dd_offsets"
KEYS = (MODELS_KEY, OFFSETS_KEY)
FORECAST_SOURCE = "Open-Meteo (archive to 6 days ago, then forecast) — Weather Settings"
FORECAST_SPREAD_F = 1.5  # the ± a 1–16 day forecast is read with when dating an event
EXTRAPOLATE_DAYS = 45    # past the forecast, at the recent daily rate; said as such, wider ±

_REF_NOTE = ("From the shipped IPM reference: its base is stated in °C (converted here) and its degree-day totals "
             "match the published °F·day models, so they are read as °F·day. No upper threshold in the reference. "
             "VERIFY against the WSU Decision Aid System / UC IPM model before relying on a date.")

#: Version 1 of `pest_dd_models`, published at install. Only the reference's own numbers, units made explicit.
DEFAULT_MODELS = {
	"schema_version": 1,
	"key": MODELS_KEY,
	"effective_from": "2020-01-01",
	"method": "daily average, horizontal cutoff: ((min(high, upper) + max(low, base)) / 2) - base, never below 0",
	"units": "°F and °F·day",
	"models": {
		"western-cherry-fruit-fly": {
			"base_f": 41.0, "upper_f": None, "biofix": "mar1",
			"events": [{"name": "first emergence", "dd": 950}, {"name": "peak emergence", "dd": 1100}],
			"citation": "ipm_reference PEST_MODELS (UC IPM; PNW handbooks)", "verify": True, "note": _REF_NOTE,
		},
		"spotted-wing-drosophila": {
			"base_f": 45.0, "upper_f": None, "biofix": "jan1",
			"events": [{"name": "first flight", "dd": 150}, {"name": "peak flight", "dd": 600}],
			"citation": "ipm_reference PEST_MODELS (MSU / PNW SWD research)", "verify": True, "note": _REF_NOTE,
		},
		"codling-moth": {
			"base_f": 50.0, "upper_f": None, "biofix": "first_catch",
			"events": [{"name": "egg hatch", "dd": 220}],
			"citation": "ipm_reference PEST_MODELS (UC IPM)", "verify": True, "note": _REF_NOTE,
		},
		"obliquebanded-leafroller": {
			"base_f": 43.0, "upper_f": None, "biofix": "first_catch",
			"events": [],
			"citation": "ipm_reference PEST_MODELS (PNW handbooks)", "verify": True,
			"note": _REF_NOTE + " The reference gives no event totals: add them (e.g. larval emergence) to date anything.",
		},
	},
}

#: Version 1 of `pest_dd_offsets`. Conservative, documented, and meant to be replaced by calibration.
DEFAULT_OFFSETS = {
	"schema_version": 1,
	"key": OFFSETS_KEY,
	"effective_from": "2020-01-01",
	"aspect_f": {"S": 1.5, "SW": 1.5, "SE": 1.0, "W": 0.5, "E": 0.0, "NW": -0.75, "NE": -0.75, "N": -1.0, "flat": 0.0},
	"full_effect_slope_deg": 15.0,
	"elevation_lapse_f_per_1000ft": -3.5,
	"max_abs_offset_f": 4.0,
	"blocks": {},
	"citation": ("Starting values, deliberately small: equator-facing slopes run warmer than the grid cell and "
	             "pole-facing slopes cooler (slope/aspect microclimate literature; the farm's own aspect layer). "
	             "Scaled by slope up to full effect at 15°; elevation by the standard environmental lapse rate "
	             "(about 3.5 °F per 1,000 ft) against the weather grid cell's elevation. Calibration replaces these "
	             "per block with values the block's own observations support."),
}


# ── the config in force ─────────────────────────────────────────────────────
_OVERLAY: contextvars.ContextVar = contextvars.ContextVar("erpnext_mcp_pest_dd_overlay", default=None)


@contextlib.contextmanager
def overlay(key: str, body: dict):
	"""During a preview only: readers take `body` for `key` instead of the published version."""
	current = dict(_OVERLAY.get() or {})
	current[key] = dict(body or {})
	token = _OVERLAY.set(current)
	try:
		yield
	finally:
		_OVERLAY.reset(token)


def _published(key: str) -> tuple[dict, str]:
	try:
		from . import phone_config

		if not phone_config.ready():
			return {}, "built-in"
		rows = phone_config.rows(KIND, key, (phone_config.PUBLISHED,))
		if not rows:
			return {}, "built-in"
		row = max(rows, key=lambda r: int(r.get("version") or 0))
		return phone_config.body_of(frappe.get_doc(phone_config.DOCTYPE, row["name"])), row["name"]
	except Exception:
		return {}, "built-in"


def models() -> tuple[dict, str]:
	staged = (_OVERLAY.get() or {}).get(MODELS_KEY)
	body, version = (staged, "preview") if staged is not None else _published(MODELS_KEY)
	return (body.get("models") if body else None) or DEFAULT_MODELS["models"], version


def offsets() -> tuple[dict, str]:
	staged = (_OVERLAY.get() or {}).get(OFFSETS_KEY)
	body, version = (staged, "preview") if staged is not None else _published(OFFSETS_KEY)
	return ({**DEFAULT_OFFSETS, **body} if body else dict(DEFAULT_OFFSETS)), version


def seed_body(key: str) -> dict:
	return dict(DEFAULT_MODELS if key == MODELS_KEY else DEFAULT_OFFSETS)


def validate(body: dict, key: str = "", for_publish: bool = False) -> dict:
	"""`{errors, warnings}` — never raises."""
	errors, warnings = [], []
	key = key or str(body.get("key") or "")
	if key == MODELS_KEY:
		for name, model in (body.get("models") or {}).items():
			if not isinstance(model, dict):
				errors.append(f"{name}: a model is an object.")
				continue
			try:
				base = float(model.get("base_f"))
			except (TypeError, ValueError):
				errors.append(f"{name}: base_f (°F) is required.")
				continue
			upper = model.get("upper_f")
			if upper not in (None, "") and float(upper) <= base:
				errors.append(f"{name}: upper_f must be above base_f.")
			if str(model.get("biofix") or "") not in ("jan1", "mar1", "first_catch") and not str(model.get("biofix") or "")[:5].count("-"):
				errors.append(f"{name}: biofix is jan1, mar1, first_catch or a month-day like 04-01.")
			last = -1.0
			for event in model.get("events") or []:
				dd = float(event.get("dd") or -1)
				if dd <= last:
					errors.append(f"{name}: event totals must rise ({event.get('name')}).")
				last = dd
			if not model.get("citation"):
				warnings.append(f"{name}: no citation.")
			if model.get("verify"):
				warnings.append(f"{name}: marked verify — dates from it are shown with that caveat.")
	elif key == OFFSETS_KEY:
		for label, value in (body.get("aspect_f") or {}).items():
			if abs(float(value)) > float(body.get("max_abs_offset_f") or 4.0):
				errors.append(f"aspect_f {label}: {value} °F is beyond max_abs_offset_f.")
		for block, entry in (body.get("blocks") or {}).items():
			if not isinstance(entry, dict) or "offset_f" not in entry:
				errors.append(f"blocks.{block}: needs offset_f.")
			elif not entry.get("reason"):
				errors.append(f"blocks.{block}: needs a reason (calibration or a person's note).")
	else:
		errors.append(f"unknown IPM Setting key {key!r}.")
	return {"errors": errors, "warnings": warnings}


# ── the block ───────────────────────────────────────────────────────────────
def _aspect_summary(block: str) -> dict | None:
	try:
		from . import slope_aspect

		meta = slope_aspect.read_meta()
		if meta is None:
			return None
		rows, _ = slope_aspect.block_summaries("", meta)
		return next((r for r in rows if r["field"] == block), None)
	except Exception:
		return None


def _block_elevation(block: str):
	try:
		from . import slope_aspect

		point = _point(block)
		return slope_aspect.elevation_at(point[0], point[1]) if point else None
	except Exception:
		return None


def block_offset(block: str, grid_elevation_m=None) -> dict:
	"""°F added to the block's daily high and low, and every reason for it."""
	table, version = offsets()
	pinned = (table.get("blocks") or {}).get(block)
	if pinned:
		return {"offset_f": round(float(pinned["offset_f"]), 2), "source": "block", "config": version,
		        "reasons": [str(pinned.get("reason"))]}
	reasons, total = [], 0.0
	summary = _aspect_summary(block)
	if summary:
		label = summary.get("mean_aspect") or "flat"
		base = float((table.get("aspect_f") or {}).get(label, 0.0))
		slope = float(summary.get("mean_slope_degrees") or 0.0)
		scale = min(1.0, slope / float(table.get("full_effect_slope_deg") or 15.0))
		part = round(base * scale, 2)
		total += part
		reasons.append(f"faces {label}, mean slope {slope:g}° → {part:+g} °F ({base:+g} °F × {scale:.2f})")
		elevation = _block_elevation(block)
		if elevation is not None and grid_elevation_m is not None:
			lapse = float(table.get("elevation_lapse_f_per_1000ft") or -3.5)
			part = round(lapse * (float(elevation) - float(grid_elevation_m)) * 3.28084 / 1000.0, 2)
			total += part
			reasons.append(f"block {elevation:.0f} m vs weather cell {float(grid_elevation_m):.0f} m → {part:+g} °F")
		elif elevation is None:
			reasons.append("elevation not in the aspect layer yet (rebuild it with build_slope_aspect_layer)")
		else:
			reasons.append("weather cell elevation not known yet — no elevation offset")
	else:
		reasons.append("no slope/aspect layer for this block (build_slope_aspect_layer) — no offset")
	cap = float(table.get("max_abs_offset_f") or 4.0)
	total = max(-cap, min(cap, total))
	return {"offset_f": round(total, 2), "source": "aspect table", "config": version, "reasons": reasons}


def _point(block: str):
	from . import ccf_providers

	return ccf_providers.block_point({"block": block, "block_doctype": "Field"}, {})


def _first_catch(block: str, pest_name: str, year: int) -> datetime.date | None:
	if not compat.doctype_exists("Crop Observation"):
		return None
	rows = frappe.db.get_all("Crop Observation", filters={"block": block, "threat": pest_name},
	                      fields=["observed_on", "count_observed"], order_by="observed_on asc", limit=500) or []
	for row in rows:
		day = row.get("observed_on")
		if day and str(day)[:4] == str(year) and float(row.get("count_observed") or 0) > 0:
			return datetime.date.fromisoformat(str(day)[:10])
	return None


def _biofix(rule: str, year: int, block: str, pest_name: str) -> tuple[datetime.date | None, str]:
	if rule == "jan1":
		return datetime.date(year, 1, 1), "1 January"
	if rule == "mar1":
		return datetime.date(year, 3, 1), "1 March"
	if rule == "first_catch":
		day = _first_catch(block, pest_name, year)
		return day, ("first trap catch logged on this block" if day else "waiting for the first trap catch on this block")
	try:
		month, day = (int(x) for x in str(rule).split("-")[:2])
		return datetime.date(year, month, day), rule
	except (TypeError, ValueError):
		return None, f"unreadable biofix {rule!r}"


def _dd(high, low, base, upper, offset) -> float | None:
	if high is None or low is None:
		return None
	high, low = float(high) + offset, float(low) + offset
	if upper not in (None, ""):
		high = min(high, float(upper))
	low = max(low, base)
	return max(0.0, (max(high, low) + low) / 2 - base)


def _crossing(series: list, start_total: float, target: float, offset_delta: float, base, upper, offset):
	total = start_total
	for day, high, low in series:
		dd = _dd(high, low, base, upper, offset + offset_delta)
		if dd is None:
			continue
		total += dd
		if total >= target:
			return day
	return None


def status(block: str, pest: str, as_of: str = "") -> dict:
	"""One pest on one block: accumulated DD from its biofix, each event reached or projected, provenance."""
	from . import degree_days, ipm_graph

	table, models_version = models()
	model = table.get(pest)
	pest_name = frappe.db.get_value(ipm_graph.ORGANISM, pest, "organism_name") if ipm_graph.installed() else None
	pest_name = pest_name or pest.replace("-", " ").title()
	if not model:
		return {"pest": pest, "block": block, "available": False,
		        "reason": f"No degree-day model for {pest_name}. Add one to the IPM Setting pest_dd_models."}
	today = datetime.date.fromisoformat(str(as_of)[:10]) if as_of else datetime.date.fromisoformat(str(frappe.utils.today())[:10])
	point = _point(block)
	if not point:
		return {"pest": pest, "block": block, "available": False, "reason": "The block has no boundary or centroid to read weather for."}
	from . import ccf_providers

	if not ccf_providers.forecast_enabled():
		return {"pest": pest, "block": block, "available": False, "reason": "Block forecasts are off (Weather Settings)."}
	start, biofix_text = _biofix(str(model.get("biofix") or "jan1"), today.year, block, pest_name)
	base, upper = float(model["base_f"]), model.get("upper_f")
	out = {"pest": pest, "pest_name": pest_name, "block": block, "available": True, "as_of": today.isoformat(),
	       "model": {k: model.get(k) for k in ("base_f", "upper_f", "biofix", "events", "citation", "verify", "note")},
	       "biofix": biofix_text, "biofix_date": start.isoformat() if start else None}
	temps = degree_days.daily_temps(point[0], point[1], start or datetime.date(today.year, 1, 1),
	                                today + datetime.timedelta(days=16))
	grid_elevation = getattr(degree_days, "LAST_ELEVATION", {}).get(ccf_providers._cell(point[0], point[1]))
	offset = block_offset(block, grid_elevation)
	out["offset"] = offset
	out["weather"] = {"source": FORECAST_SOURCE, "grid_cell": ccf_providers._cell(point[0], point[1]),
	                  "latitude": round(point[0], 5), "longitude": round(point[1], 5),
	                  "grid_elevation_m": grid_elevation}
	out["config"] = {"pest_dd_models": models_version, "pest_dd_offsets": offset["config"]}
	if not start:
		out.update({"dd_to_date": None, "events": [], "window_open": False,
		            "summary": f"{pest_name}: {biofix_text}."})
		return out
	days = sorted(temps.items())
	past = [(datetime.date.fromisoformat(d), hl[0], hl[1]) for d, hl in days if datetime.date.fromisoformat(d) <= today]
	ahead = [(datetime.date.fromisoformat(d), hl[0], hl[1]) for d, hl in days if datetime.date.fromisoformat(d) > today]
	total, missing, reached_on = 0.0, 0, {}
	targets = [(e["name"], float(e["dd"])) for e in model.get("events") or []]
	for day, high, low in past:
		if day < start:
			continue
		dd = _dd(high, low, base, upper, offset["offset_f"])
		if dd is None:
			missing += 1
			continue
		total += dd
		for name, need in targets:
			if name not in reached_on and total >= need:
				reached_on[name] = day
	out["weather"]["missing_days"] = missing
	out["dd_to_date"] = round(total, 1)
	recent = [d for d in (_dd(h, l, base, upper, offset["offset_f"]) for _, h, l in (past[-7:] + ahead)) if d is not None]
	rate = sum(recent) / len(recent) if recent else 0.0
	forecast_total = total + sum(d for d in (_dd(h, l, base, upper, offset["offset_f"]) for _, h, l in ahead) if d is not None)
	events = []
	for name, need in targets:
		if name in reached_on:
			events.append({"name": name, "dd": need, "status": "reached", "date": reached_on[name].isoformat(),
			               "plus_minus_days": 0, "text": f"{pest_name} {name}: reached {reached_on[name]:%b %-d}."})
			continue
		mid = _crossing(ahead, total, need, 0.0, base, upper, offset["offset_f"])
		if mid:
			early = _crossing(ahead, total, need, FORECAST_SPREAD_F, base, upper, offset["offset_f"]) or mid
			late = _crossing(ahead, total, need, -FORECAST_SPREAD_F, base, upper, offset["offset_f"]) or (mid + datetime.timedelta(days=3))
			pm = max(1, max((mid - early).days, (late - mid).days))
			events.append({"name": name, "dd": need, "status": "projected", "date": mid.isoformat(), "plus_minus_days": pm,
			               "text": f"{pest_name} {name} expected ~{mid:%b %-d} ±{pm} d (forecast)."})
			continue
		if rate > 0:
			days_more = math.ceil((need - forecast_total) / rate)
			last_day = ahead[-1][0] if ahead else today
			if days_more <= EXTRAPOLATE_DAYS:
				when = last_day + datetime.timedelta(days=days_more)
				pm = max(3, round(days_more * 0.3))
				events.append({"name": name, "dd": need, "status": "beyond_forecast", "date": when.isoformat(),
				               "plus_minus_days": pm,
				               "text": f"{pest_name} {name}: ~{when:%b %-d} ±{pm} d, past the forecast at the recent rate — rough."})
				continue
		events.append({"name": name, "dd": need, "status": "not_yet", "date": None, "plus_minus_days": None,
		               "text": f"{pest_name} {name}: {round(need - total)} °F·day to go — beyond the forecast."})
	out["events"] = events
	out["window_open"] = bool(targets) and targets[0][0] in reached_on
	head = next((e for e in events if e["status"] != "reached"), events[-1] if events else None)
	out["summary"] = (f"{pest_name}: {out['dd_to_date']:g} °F·day since {biofix_text} (base {base:g} °F, offset "
	                  f"{offset['offset_f']:+g} °F). " + (head["text"] if head else "No events in the model.") +
	                  (" Model marked verify." if model.get("verify") else ""))
	return out


def statuses(block: str, as_of: str = "") -> list[dict]:
	table, _ = models()
	return [status(block, pest, as_of) for pest in sorted(table)]


# ── calibration: compare, suggest, never apply ──────────────────────────────
def suggest_calibration(block: str, pest: str, observed_on: str, event: str = "", as_of: str = "") -> dict:
	"""What offset would have put the model's event on the day it was observed on this block.

	Returns the suggestion; `draft_offset` writes it as a DRAFT version of pest_dd_offsets for a person to
	publish. Nothing here changes the version in force."""
	from . import degree_days

	table, _ = models()
	model = table.get(pest)
	if not model or not model.get("events"):
		raise ValueError(f"no degree-day model with events for {pest!r}.")
	target = next((e for e in model["events"] if not event or e["name"] == event), None)
	if not target:
		raise ValueError(f"{pest} has no event {event!r}.")
	seen = datetime.date.fromisoformat(str(observed_on)[:10])
	point = _point(block)
	if not point:
		raise ValueError("the block has no boundary or centroid.")
	start, _text = _biofix(str(model.get("biofix") or "jan1"), seen.year, block, pest)
	if not start or start > seen:
		raise ValueError("no biofix before the observed date.")
	temps = degree_days.daily_temps(point[0], point[1], start, seen)
	current = block_offset(block)["offset_f"]
	base, upper = float(model["base_f"]), model.get("upper_f")

	def total_at(offset):
		return sum(d for d in (_dd(h, l, base, upper, offset) for day, (h, l) in sorted(temps.items())
		                       if start <= datetime.date.fromisoformat(day) <= seen) if d is not None)

	lo, hi = -6.0, 6.0
	for _ in range(40):  # the offset whose accumulated DD on the observed day equals the event total
		mid = (lo + hi) / 2
		if total_at(mid) < float(target["dd"]):
			lo = mid
		else:
			hi = mid
	suggested = round((lo + hi) / 2, 2)
	return {"block": block, "pest": pest, "event": target["name"], "observed_on": seen.isoformat(),
	        "dd_at_observed_with_current": round(total_at(current), 1), "event_dd": target["dd"],
	        "current_offset_f": current, "suggested_offset_f": suggested,
	        "change_f": round(suggested - current, 2),
	        "note": "A suggestion from one observation. Several observations across seasons agreeing is what makes "
	                "an offset trustworthy; a person publishes it."}


def calibrate(block: str, observations: list[dict], author: str = "", draft: bool = False) -> dict:
	"""Several observations on one block → one suggested block offset (the median), optionally written as a
	DRAFT of pest_dd_offsets for a person to publish. `observations`: [{pest, observed_on, event?}] — a pest
	event (first trap catch = the model's first event) or, once a crop model exists in pest_dd_models, a crop
	event such as harvest (Tim's historical harvest dates by block). NEVER PUBLISHES."""
	rows, refused = [], []
	for obs in observations or []:
		try:
			rows.append(suggest_calibration(block, str(obs.get("pest") or obs.get("model") or ""),
			                                str(obs.get("observed_on") or obs.get("date") or ""), str(obs.get("event") or "")))
		except (ValueError, TypeError) as exc:
			refused.append({"observation": obs, "why": str(exc)})
	if not rows:
		return {"block": block, "suggestions": [], "refused": refused, "suggested_offset_f": None,
		        "note": "Nothing to compare: log observations (trap catches, emergence, harvest dates) on this block."}
	values = [r["suggested_offset_f"] for r in rows]
	median = round(statistics.median(values), 2)
	spread = round(max(values) - min(values), 2) if len(values) > 1 else None
	out = {"block": block, "suggestions": rows, "refused": refused, "suggested_offset_f": median,
	       "spread_f": spread, "observations_used": len(rows),
	       "confidence": "low" if len(rows) < 3 or (spread or 0) > 2.0 else "moderate" if len(rows) < 6 else "good"}
	if draft:
		from . import phone_config

		table, version = offsets()
		body = {k: v for k, v in table.items() if k not in ("schema_version",)}
		blocks = dict(body.get("blocks") or {})
		blocks[block] = {"offset_f": median,
		                 "reason": f"calibration from {len(rows)} observation(s) (spread {spread if spread is not None else 'n/a'} °F), "
		                           + ", ".join(" ".join((r["pest"], r["event"], r["observed_on"])) for r in rows[:6]),
		                 "calibrated_on": str(frappe.utils.today()), "replaces_config": version}
		body["blocks"] = blocks
		doc, _report = phone_config.save_draft(KIND, OFFSETS_KEY, {"schema_version": 1, **body, "key": OFFSETS_KEY},
		                                       f"Calibration suggestion for {block} by {author or 'calibrate'}.", "AI-proposed")
		out["draft"] = phone_config.describe(doc)
		out["next"] = "A person compares it with preview_config kind pest_dd_offsets and publishes it in the Desk."
	return out


def preview(key: str, body, blocks: list | None = None) -> dict:
	"""Each block's offset and next pest event under the version in force and under `body`. Writes nothing."""
	if key not in KEYS:
		raise ValueError(f"key is one of {', '.join(KEYS)}.")
	if not isinstance(body, dict):
		raise ValueError(f"body is the whole {key} to try.")
	report = validate(body, key, for_publish=True)
	names = blocks or [r["name"] for r in frappe.db.get_all("Field", fields=["name"], order_by="name asc", limit=25)]
	rows = []
	for block in names:
		now = statuses(block)
		with overlay(key, body):
			then = statuses(block)
		for a, b in zip(now, then):
			if not (a.get("available") or b.get("available")):
				continue
			head = lambda st: next((e for e in st.get("events") or [] if e["status"] != "reached"), None)
			rows.append({"block": block, "pest": a["pest"],
			             "offset_now": (a.get("offset") or {}).get("offset_f"), "offset_draft": (b.get("offset") or {}).get("offset_f"),
			             "next_now": (head(a) or {}).get("date"), "next_draft": (head(b) or {}).get("date")})
	return {"validation": report, "blocks": rows, "written": False,
	        "note": "Blocks shown: those named, else the first 25 by name." if not blocks else ""}


def logged_observations(block: str) -> list[dict]:
	"""What this block's own records can calibrate against: each year's first trap catch of a pest whose model
	starts with an emergence / flight event."""
	from . import ipm_graph

	table, _ = models()
	out = []
	for pest, model in table.items():
		first = (model.get("events") or [{}])[0].get("name", "")
		if model.get("biofix") == "first_catch" or not any(w in first for w in ("emergence", "flight")):
			continue
		name = frappe.db.get_value(ipm_graph.ORGANISM, pest, "organism_name") if ipm_graph.installed() else None
		if not name or not compat.doctype_exists("Crop Observation"):
			continue
		seen = {}
		for row in frappe.db.get_all("Crop Observation", filters={"block": block, "threat": name},
		                          fields=["observed_on", "count_observed"], order_by="observed_on asc", limit=2000) or []:
			day = str(row.get("observed_on") or "")[:10]
			if day and float(row.get("count_observed") or 0) > 0:
				seen.setdefault(day[:4], day)
		out.extend({"pest": pest, "event": first, "observed_on": day} for day in seen.values())
	return out


# ── the CCF provider: pest windows on a block, for Go / Hold and rules ──────
def _slug(pest: str) -> str:
	return pest.replace("-", "_")


def provider_values(subject: dict, ctx: dict) -> dict:
	block = ""
	if str(subject.get("location_doctype") or "") == "Field":
		block = str(subject.get("location") or "")
	elif subject.get("block"):
		block = str(subject["block"])
	elif ctx.get("doctype") == "Field":
		block = str(subject.get("name") or "")
	if not block:
		return {}
	out = {}
	for st in statuses(block, str(ctx.get("as_of_date") or "")):
		if not st.get("available"):
			continue
		key = _slug(st["pest"])
		head = next((e for e in st.get("events") or [] if e["status"] != "reached"), None)
		out[f"{key}_dd"] = st.get("dd_to_date")
		out[f"{key}_window_open"] = bool(st.get("window_open"))
		out[f"{key}_days_to_next"] = (
			(datetime.date.fromisoformat(head["date"]) - datetime.date.fromisoformat(st["as_of"])).days
			if head and head.get("date") else None)
	return out


def register(ccf) -> None:
	p = ccf._p
	params = {}
	for pest in DEFAULT_MODELS["models"]:
		key = _slug(pest)
		label = pest.replace("-", " ")
		params[f"{key}_dd"] = p("number", f"{label}: °F·day since its biofix on this block (farm weather + block offset).", "°F·day", 600)
		params[f"{key}_window_open"] = p("bool", f"{label}: its first model event has been reached on this block.", example=True)
		params[f"{key}_days_to_next"] = p("number", f"{label}: days until the next model event (forecast; null past it).", "days", 5)
	ccf.register(ccf.Provider(
		"pest_dd", params, lambda subject, ctx: provider_values(subject, ctx), past=False,
		description="Pest degree-day windows per block (v0.264.0): Open-Meteo weather, slope / aspect / elevation offset.",
	))


# ── v0.264.1: harvest windows → a proposed crop model and parcel offsets (Drafts only) ──
HARVEST_SEED = "seed_data/harvest_windows_highland.json"
WEAK_YEARS = {2023: 0.3}  # 2023: market failure at Mill Creek — picked when it was worth picking, not when ripe


def harvest_windows() -> dict:
	import json
	import os

	with open(os.path.join(os.path.dirname(__file__), HARVEST_SEED), encoding="utf-8") as handle:
		return json.load(handle)


def _dd_between(block: str, start: datetime.date, end: datetime.date, base: float, upper) -> float | None:
	from . import degree_days

	point = _point(block)
	if not point:
		return None
	temps = degree_days.daily_temps(point[0], point[1], start, end)
	total, seen = 0.0, 0
	for day, (high, low) in sorted(temps.items()):
		if start <= datetime.date.fromisoformat(day) <= end:
			dd = _dd(high, low, base, upper, 0.0)
			if dd is not None:
				total += dd
				seen += 1
	return round(total, 1) if seen else None


def propose_harvest_calibration(block_map: dict, base_f: float = 40.0, upper_f=86.0, biofix: str = "mar1",
                                author: str = "", draft: bool = False, windows: list | None = None) -> dict:
	"""Fit a 'harvest start' degree-day total per variety from the farm's own picking windows (raw weather, no
	offset), then each parcel's RELATIVE offset from how far its windows sit from those totals.

	THE FARM'S DATA, NOT LITERATURE: the totals are what this farm's own weather summed to on the days it started
	picking. RELATIVE, NOT ABSOLUTE: the totals come from the same windows, so only the difference between parcels
	is identified — the offsets are centred on zero. WEAK: picking also follows the market and the crew (2023 is
	down-weighted). Everything comes back as a proposal; draft=True writes DRAFT versions of pest_dd_models and
	pest_dd_offsets for a person to publish. Nothing is applied."""
	data = windows if windows is not None else harvest_windows()["windows"]
	rows, skipped = [], []
	for w in data:
		block = (block_map or {}).get(w["parcel"])
		if not block:
			skipped.append({**w, "why": f"no block mapped for parcel {w['parcel']!r}"})
			continue
		start, _ = _biofix(biofix, int(w["year"]), block, "")
		harvest = datetime.date.fromisoformat(w["harvest_start"])
		dd = _dd_between(block, start, harvest, base_f, upper_f) if start else None
		if dd is None:
			skipped.append({**w, "why": "no weather for this block / window"})
			continue
		rows.append({**w, "block": block, "dd_at_harvest_start": dd, "days": (harvest - start).days + 1,
		             "weight": WEAK_YEARS.get(int(w["year"]), 1.0)})
	by_variety: dict = {}
	for r in rows:
		by_variety.setdefault(r["variety"], []).append(r)
	models = {}
	for variety, group in sorted(by_variety.items()):
		weight = sum(r["weight"] for r in group)
		mean = sum(r["dd_at_harvest_start"] * r["weight"] for r in group) / weight
		spread = max(r["dd_at_harvest_start"] for r in group) - min(r["dd_at_harvest_start"] for r in group)
		models[f"sweet-cherry-{variety.lower().replace(' ', '-')}"] = {
			"base_f": base_f, "upper_f": upper_f, "biofix": biofix, "kind": "crop",
			"events": [{"name": "harvest start", "dd": round(mean)}],
			"citation": f"Fitted from this farm's harvest windows ({len(group)} season-parcel(s), spread {round(spread)} °F·day) "
			            "on Open-Meteo weather — Harvest_Dates.numbers, Highland LLC.",
			"verify": True,
			"note": "Picking dates follow the market and crews as well as ripeness; 2023 down-weighted (market failure).",
		}
	residuals: dict = {}
	for r in rows:
		model = models[f"sweet-cherry-{r['variety'].lower().replace(' ', '-')}"]
		residual = r["dd_at_harvest_start"] - model["events"][0]["dd"]
		residuals.setdefault(r["parcel"], []).append((residual / r["days"], r["weight"]))
	raw = {p: -sum(v * w for v, w in vals) / sum(w for _, w in vals) for p, vals in residuals.items()}
	centre = sum(raw.values()) / len(raw) if raw else 0.0
	proposed_offsets = {block_map[p]: {"offset_f": round(v - centre, 2),
	                          "reason": f"relative harvest-window calibration ({p}: {len(residuals[p])} window(s)); "
	                                    "parcels compared with each other, centred on zero"}
	           for p, v in raw.items()}
	out = {"windows_used": rows, "skipped": skipped, "proposed_models": models, "proposed_block_offsets": proposed_offsets,
	       "note": ("Proposals only. The block offsets are relative between parcels; the crop models are this farm's "
	                "own picking dates in degree days. A person reviews them with preview_config and publishes.")}
	if draft and models:
		from . import phone_config

		current, _ = _published(MODELS_KEY)
		body = {**DEFAULT_MODELS, **(current or {})}
		body["models"] = {**(body.get("models") or {}), **models}
		doc, _r = phone_config.save_draft(KIND, MODELS_KEY, {**body, "key": MODELS_KEY, "schema_version": 1},
		                                  f"Harvest-window crop models proposed by {author or 'calibration'}.", "AI-proposed")
		out["models_draft"] = phone_config.describe(doc)
		table, _v = offsets()
		obody = {k: v for k, v in table.items() if k != "schema_version"}
		obody["blocks"] = {**(obody.get("blocks") or {}), **proposed_offsets}
		doc, _r = phone_config.save_draft(KIND, OFFSETS_KEY, {**obody, "key": OFFSETS_KEY, "schema_version": 1},
		                                  f"Harvest-window parcel offsets proposed by {author or 'calibration'}.", "AI-proposed")
		out["offsets_draft"] = phone_config.describe(doc)
	return out

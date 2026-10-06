# SPDX-License-Identifier: MIT
"""Did it actually rain? The archive check on weather-gated work. v0.253.0. Tim's decision 21
("continuous, at least weekly, comparing what actually happened"); design §4 ("what actually happened"
a week after wounding work).

* WHAT IS CHECKED: a COMPLETED Farm Task whose Go / Hold log holds a rule's rain bet (`rain_forecast`,
  written by `go_hold` since v0.253.0: the window, the threshold in inches and the forecast % chance of a
  day over it). The bet taken is the one the work started under — the task-start check, else the last
  check before it.
* WHAT ACTUALLY FELL: daily rain for the block's centroid (cached per H3 cell), from Open-Meteo's archive
  once the window is six days behind (the archive runs about five days late) — FINAL — and before that from
  the forecast API's past days — PROVISIONAL, replaced by the archive later. Recorded on the task's
  `go_hold_log` as moment `archive_check`, so the evidence sits beside the decision it judges.
* CCF, NOT CODE: the `weather_check` provider publishes the result, and a seeded rule (OFF) alerts when
  rain fell inside the window after work that needed it dry ("check the cuts"). The farm tunes or copies it.
* THE FORECAST IS SCORED: `summary()` (the `get_forecast_verification` tool) — how often it rained against
  the forecast chance, the Brier score, and a reliability table. A 30% forecast should see rain about
  three times in ten.

Daily at 04:45; never raises; does nothing with block forecasts off.
"""

from __future__ import annotations

import datetime
import json
import time

import frappe

from . import ccf_providers, compat

DOCTYPE = "Farm Task"
MOMENT = "archive_check"
ARCHIVE_LAG_DAYS = 6
LOOKBACK_DAYS = 60
TTL_SECONDS = 6 * 3600
BINS = ((0, 20), (20, 40), (40, 60), (60, 80), (80, 101))
_CACHE: dict = {}


def _today() -> datetime.date:
	return datetime.date.fromisoformat(str(frappe.utils.today())[:10])


def _date(value) -> datetime.date | None:
	try:
		return datetime.date.fromisoformat(str(value or "")[:10])
	except ValueError:
		return None


def _log(raw) -> list:
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
	except ValueError:
		return []
	return value if isinstance(value, list) else []


def bet(log: list) -> dict | None:
	"""The rain bet the work started under: the task-start entry's, else the newest before it."""
	entries = [e for e in log if isinstance(e, dict) and e.get("moment") not in (MOMENT, "override")]
	start = next((e for e in entries if e.get("moment") == "task_start"), None)
	ordered = ([start] if start else []) + entries
	for entry in ordered:
		for rule in entry.get("rules") or []:
			forecast = rule.get("rain_forecast")
			if forecast and forecast.get("from"):
				return {**forecast, "rule_id": rule.get("rule_id"), "rule_version": rule.get("version"),
				        "decided_at": entry.get("at"), "decided_moment": entry.get("moment")}
	return None


def actual_daily(lat: float, lon: float, start: datetime.date, end: datetime.date) -> tuple:
	"""({date: inches}, source, final). Archive when `end` is ARCHIVE_LAG_DAYS behind, else the forecast
	API's past days (provisional). ({}, …) on any failure."""
	from .services import weather

	today = _today()
	final = end <= today - datetime.timedelta(days=ARCHIVE_LAG_DAYS)
	cell = ccf_providers._cell(lat, lon)
	key = (cell, start.isoformat(), end.isoformat(), final)
	hit = _CACHE.get(key)
	if hit and hit[0] > time.time():
		return hit[1]
	if final:
		payload = weather._get_json(
			weather.base_url("archive"),
			{"latitude": lat, "longitude": lon, "start_date": start.isoformat(), "end_date": end.isoformat(),
			 "daily": "precipitation_sum", "timezone": "auto", "precipitation_unit": "inch"},
			f"rain-check:{cell}",
		)
		source = "Open-Meteo archive"
	else:
		payload = weather._get_json(
			weather.base_url("current"),
			{"latitude": lat, "longitude": lon, "daily": "precipitation_sum", "timezone": "auto",
			 "precipitation_unit": "inch", "past_days": min(92, (today - start).days + 1), "forecast_days": 1},
			f"rain-check:{cell}",
		)
		source = "Open-Meteo forecast API, past days (provisional)"
	daily = (payload or {}).get("daily") or {} if isinstance(payload, dict) else {}
	days = list(daily.get("time") or [])
	amounts = list(daily.get("precipitation_sum") or [None] * len(days))
	out = {}
	for index, day in enumerate(days):
		when = _date(day)
		if when and start <= when <= end:
			out[day] = ccf_providers._num(amounts[index] if index < len(amounts) else None)
	result = (out, source, final)
	if out:
		_CACHE[key] = (time.time() + TTL_SECONDS, result)
	return result


def verify_task(name: str) -> dict | None:
	"""Check one completed task; record the result on its log. None when there is nothing to check yet."""
	row = frappe.db.get_value(DOCTYPE, name, ["name", "state", "location_doctype", "location", "go_hold_log"], as_dict=True)
	if not row or str(row.get("state") or "") != "Completed":
		return None
	log = _log(row.get("go_hold_log"))
	done = next((e for e in log if isinstance(e, dict) and e.get("moment") == MOMENT), None)
	if done and done.get("final"):
		return None
	wager = bet(log)
	if not wager:
		return None
	start, end = _date(wager["from"]), _date(wager.get("to"))
	if not start or not end or end >= _today():
		return None
	point = ccf_providers.block_point(dict(row), {"doctype": DOCTYPE})
	if not point:
		return None
	amounts, source, final = actual_daily(point[0], point[1], start, end)
	known = {day: amount for day, amount in amounts.items() if amount is not None}
	if not known:
		return None
	over = float(wager.get("over_in") or ccf_providers.WET_DAY_IN)
	wet = sorted(day for day, amount in known.items() if amount > over)
	rained = bool(wet)
	risk = float(wager.get("risk_pct") or 0) / 100.0
	entry = {
		"at": frappe.utils.now(),
		"moment": MOMENT,
		"final": bool(final and len(known) >= int(wager.get("days") or len(known))),
		"source": source,
		"window": [start.isoformat(), end.isoformat()],
		"over_in": over,
		"forecast_risk_pct": wager.get("risk_pct"),
		"rule_id": wager.get("rule_id"),
		"rule_version": wager.get("rule_version"),
		"decided_at": wager.get("decided_at"),
		"actual_daily": [{"date": day, "precip_in": known[day]} for day in sorted(known)],
		"days_observed": len(known),
		"wet_days": wet,
		"max_in": max(known.values()),
		"total_in": round(sum(known.values()), 2),
		"rained": rained,
		"brier": round((risk - (1.0 if rained else 0.0)) ** 2, 4),
	}
	log = [e for e in log if not (isinstance(e, dict) and e.get("moment") == MOMENT)]
	log.insert(0, entry)
	frappe.db.set_value(DOCTYPE, name, "go_hold_log", json.dumps(log, default=str), update_modified=False)
	return entry


def run(today: str = "") -> dict:
	"""Every completed task in the look-back with a rain bet and no FINAL check yet."""
	report = {"checked": 0, "rained": 0, "provisional": 0, "failed": 0}
	if not compat.has_field(DOCTYPE, "go_hold_log") or not ccf_providers.forecast_enabled():
		return report
	since = (_today() - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()
	rows = frappe.db.get_all(
		DOCTYPE,
		filters={"state": "Completed", "completed_at": (">=", since), "go_hold_log": ("like", "%rain_forecast%")},
		fields=["name"],
		limit=2000,
	) or []
	for row in rows:
		try:
			entry = verify_task(row["name"])
		except Exception:
			report["failed"] += 1
			frappe.log_error(title="rain archive check failed", message=frappe.get_traceback())
			continue
		if entry:
			report["checked"] += 1
			report["rained"] += bool(entry["rained"])
			report["provisional"] += not entry["final"]
	return report


def daily() -> None:
	"""The 04:45 cron entry. Never raises."""
	try:
		run()
	except Exception:
		frappe.log_error(title="rain archive check failed", message=frappe.get_traceback())


# ── scoring the forecast ────────────────────────────────────────────────────
def summary(days: int = 90, rule_id: str = "", block: str = "", company: str = "") -> dict:
	since = (_today() - datetime.timedelta(days=int(days))).isoformat()
	filters = {"state": "Completed", "completed_at": (">=", since), "go_hold_log": ("like", f"%{MOMENT}%")}
	if block:
		filters["location"] = block
	if company:
		filters["company"] = company
	rows = frappe.db.get_all(DOCTYPE, filters=filters, fields=["name", "task_name", "location", "completed_at", "go_hold_log"],
	                         order_by="completed_at desc", limit=2000) or []
	checks = []
	for row in rows:
		entry = next((e for e in _log(row.get("go_hold_log")) if isinstance(e, dict) and e.get("moment") == MOMENT), None)
		if not entry or (rule_id and entry.get("rule_id") != rule_id):
			continue
		checks.append({"task": row["name"], "task_name": row.get("task_name"), "block": row.get("location"),
		               "completed_at": str(row.get("completed_at") or "")[:19] or None,
		               **{k: entry.get(k) for k in ("window", "over_in", "forecast_risk_pct", "rained", "wet_days",
		                                             "max_in", "total_in", "final", "source", "rule_id", "brier")}})
	n = len(checks)
	reliability = []
	for low, high in BINS:
		inside = [c for c in checks if low <= float(c.get("forecast_risk_pct") or 0) < high]
		if inside:
			reliability.append({"forecast_pct": f"{low}–{min(high, 100)}", "tasks": len(inside),
			                    "mean_forecast_pct": round(sum(float(c["forecast_risk_pct"] or 0) for c in inside) / len(inside), 1),
			                    "rained_pct": round(100 * sum(1 for c in inside if c["rained"]) / len(inside), 1)})
	return {
		"days": int(days),
		"tasks_checked": n,
		"final": sum(1 for c in checks if c.get("final")),
		"rained": sum(1 for c in checks if c.get("rained")),
		"mean_forecast_pct": round(sum(float(c.get("forecast_risk_pct") or 0) for c in checks) / n, 1) if n else None,
		"observed_pct": round(100 * sum(1 for c in checks if c.get("rained")) / n, 1) if n else None,
		"brier": round(sum(float(c.get("brier") or 0) for c in checks) / n, 4) if n else None,
		"brier_note": "0 is perfect; always saying 50% scores 0.25. Lower is better.",
		"reliability": reliability,
		"checks": checks[:200],
	}


# ── the CCF provider ────────────────────────────────────────────────────────
def _values(subject: dict, ctx: dict) -> dict:
	raw = subject.get("go_hold_log")
	if raw is None and subject.get("name") and ctx.get("doctype") == DOCTYPE:
		raw = frappe.db.get_value(DOCTYPE, subject["name"], "go_hold_log")
	entry = next((e for e in _log(raw) if isinstance(e, dict) and e.get("moment") == MOMENT), None)
	if not entry:
		return {"verified": False}
	start = _date((entry.get("window") or [None])[0])
	judged = ctx.get("as_of_date") or _today()
	return {
		"verified": True,
		"final": bool(entry.get("final")),
		"rained": bool(entry.get("rained")),
		"max_in": entry.get("max_in"),
		"total_in": entry.get("total_in"),
		"wet_day_count": len(entry.get("wet_days") or []),
		"first_wet_day": (entry.get("wet_days") or [None])[0],
		"over_in": entry.get("over_in"),
		"forecast_risk_pct": entry.get("forecast_risk_pct"),
		"window_start": start.isoformat() if start else None,
		"days_since_work": (judged - start).days if start else None,
	}


def register(ccf) -> None:
	p = ccf._p
	ccf.register(
		ccf.Provider(
			"weather_check",
			{
				"verified": p("bool", "An archive check has been made for this task's rain window.", example=True),
				"final": p("bool", "From the archive (final), not the forecast API's past days (provisional).", example=True),
				"rained": p("bool", "A day in the window rained more than the rule's threshold.", example=False),
				"max_in": p("number", "Wettest day in the window.", "in", 0.02),
				"total_in": p("number", "Rain over the window.", "in", 0.03),
				"wet_day_count": p("number", "Days over the threshold.", "days", 0),
				"first_wet_day": p("date", "The first of them.", example="2026-01-17"),
				"over_in": p("number", "The threshold the rule used.", "in", 0.05),
				"forecast_risk_pct": p("number", "What the forecast said when the work started.", "%", 22),
				"window_start": p("date", "First day of the window.", example="2026-01-12"),
				"days_since_work": p("number", "Days from the window's start to the day judged.", "days", 9),
			},
			_values,
			past=True,
			description="Did it actually rain after weather-gated work? Open-Meteo archive, decision 21.",
		)
	)


# ── the seeded rule (OFF; decision 5) ───────────────────────────────────────
RULE_ID = "weather_check_pruning_rain"


def rule_spec() -> dict:
	return {
		"rule_id": RULE_ID,
		"title": "Pruning: it rained in the week after",
		"category": "Work Timing",
		"target_doctype": DOCTYPE,
		"condition_tree": {
			"id": "stayed_dry",
			"any": [
				{"path": "weather_check.verified", "op": "isfalse"},
				{"path": "weather_check.days_since_work", "op": "gt", "value": 30},
				{"path": "weather_check.rained", "op": "isfalse"},
			],
			"basis": "published",
			"source": "PNW Plant Disease Management Handbook: bacterial canker enters pruning wounds in wet weather",
			"reason": {"en": "It rained after the pruning — check the cuts for canker",
			           "es": "Llovió después de la poda — revisa los cortes por cancro"},
		},
		"actions": [{"type": "alert"}],
		"scope_filters": [{"field": "state", "op": "eq", "value": "Completed"},
		                  {"field": "task_name", "op": "contains", "value": "prun"}],
		"message_template": "{{ task_name }} ({{ location }}): rain fell in the week after the work, although the "
		                    "forecast allowed it. Check the cuts for bacterial canker.",
		"kairotic_gate_description": "Raised once the archive shows a day over the rule's threshold within the window "
		                            "the pruning was released into; quiet after 30 days.",
		"regimes": ["Internal"],
		"enabled": 0,
		"authored_by": "System",
		"purpose": "Close the loop on a forecast-gated decision: the forecast said dry; did it stay dry?",
		"default_severity": "Warning",
	}


def seed() -> list:
	"""Create-only; never raises."""
	from . import compliance_rules

	if not compat.doctype_exists(compliance_rules.DOCTYPE):
		return []
	try:
		if frappe.db.exists(compliance_rules.DOCTYPE, {"rule_id": RULE_ID}):
			return []
		compliance_rules.build_rule(rule_spec()).insert(ignore_permissions=True)
		return [RULE_ID]
	except Exception:
		frappe.log_error(title=f"{RULE_ID} not seeded", message=frappe.get_traceback())
		return []

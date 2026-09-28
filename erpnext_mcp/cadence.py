# SPDX-License-Identifier: MIT
"""Grouped, seasonal, tiered, state-driven cadence — a rule option any rule can use. v0.205.0.

docs/design/programs_and_field_kinds.md A4. Rodent bait checks were the first
("every 7 days while active or in season; every 30 off season for a quiet
maintenance station; a new placement's 10 days are always active"); nothing in
here knows about rodents. A declarative Compliance Rule carrying
`extra_parameters.grouped_cadence` is scanned by `scan`, and
`next_due` answers one group for the calendar.

A GROUP is the target rows sharing `group_by` (the Dynamic Link pair
`location_doctype`/`location` when it is `location`). Within it: the ROUND is
what follows the latest `end_filters` row; the START is the round's latest
`start_filters` row (none — nothing is out, nothing raises); the ANCHOR is the
round's latest `anchor_filters` row. ACTIVE when the anchor is within
`knockdown_days` of the start or its `state.field` is active; otherwise
MAINTENANCE, whose interval depends on the tier and the season.
"""

from __future__ import annotations

import datetime

import frappe

from . import compat, compliance_rules, occupancy

DEFAULTS = {
	"group_by": "location",
	"date_field": "completed_at",
	"knockdown_days": 0,
	"active_interval_days": 7,
	"tier": "none",
	"intervals": {"default": {"in_season_days": 7, "off_season_days": 7}},
	"season": "none",
	"state": {},
}


def params_of(raw: dict) -> dict:
	"""The block with defaults filled and its filter lists parsed. Raises ValueError."""
	params = {**DEFAULTS, **(raw or {})}
	for key in ("anchor_filters", "start_filters", "end_filters"):
		value = params.get(key) or []
		params[key] = compliance_rules.parse_filters(value, f"grouped_cadence.{key}") if value else []
	if not params["anchor_filters"]:
		raise ValueError("grouped_cadence.anchor_filters is required — which rows the clock runs from.")
	tier = str(params.get("tier") or "none")
	if tier not in ("occupancy", "none") and not tier.startswith("field:"):
		raise ValueError("grouped_cadence.tier must be occupancy, none or field:<name>.")
	if str(params.get("season") or "none") not in ("company", "none"):
		raise ValueError("grouped_cadence.season must be company or none.")
	return params


def _fields_of(params: dict) -> list:
	from .alerts.engine import _filter_fieldnames

	fields = {"name", "company", params["date_field"], "modified", "creation"}
	if params["group_by"] == "location":
		fields |= {"location_doctype", "location"}
	else:
		fields.add(params["group_by"])
	state_field = (params.get("state") or {}).get("field")
	if state_field:
		fields.add(state_field)
	if str(params.get("tier") or "").startswith("field:"):
		fields.add(params["tier"].split(":", 1)[1])
	for key in ("anchor_filters", "start_filters", "end_filters"):
		fields |= set(_filter_fieldnames(params[key]))
	return sorted(fields)


def _key(row: dict, params: dict) -> tuple:
	if params["group_by"] == "location":
		return (str(row.get("location_doctype") or ""), str(row.get("location") or ""))
	return ("", str(row.get(params["group_by"]) or ""))


def _stamp(row: dict, params: dict) -> str:
	return str(row.get(params["date_field"]) or "")


def _days(start: str, end: str) -> int:
	return (datetime.date.fromisoformat(end[:10]) - datetime.date.fromisoformat(start[:10])).days


def groups(doctype: str, params: dict, company: str = "", extra_filters: dict | None = None) -> dict:
	"""`{group key: [rows, oldest first]}` for the target doctype."""
	fields = compat.existing_fields(doctype, _fields_of(params))
	filters = dict(extra_filters or {})
	if company and "company" in fields:
		filters["company"] = company
	out: dict = {}
	for row in frappe.db.get_all(doctype, filters=filters, fields=fields, limit=20000) or []:
		row = dict(row)
		key = _key(row, params)
		if not key[1]:
			continue
		out.setdefault(key, []).append(row)
	for rows in out.values():
		rows.sort(key=lambda r: _stamp(r, params) or str(r.get("creation") or ""))
	return out


def _matches(row: dict, filters: list) -> bool:
	if not filters:
		return False
	matched, _warnings = compliance_rules.row_matches(row, filters, set(row.keys()))
	return matched


def evaluate(rows: list, params: dict, key: tuple, today: str) -> dict | None:
	"""One group's next due, or None when nothing is out."""
	ended = [_stamp(r, params) for r in rows if _stamp(r, params) and _matches(r, params["end_filters"])]
	since = max(ended) if ended else ""
	current = [r for r in rows if _stamp(r, params) and _stamp(r, params) > since]
	starts = (
		[r for r in current if _matches(r, params["start_filters"])] if params["start_filters"] else current
	)
	anchors = [r for r in current if _matches(r, params["anchor_filters"])]
	if not starts or not anchors:
		return None
	anchor_row = max(anchors, key=lambda r: _stamp(r, params))
	anchor = _stamp(anchor_row, params)
	started = max(_stamp(r, params) for r in starts)
	company = str(anchor_row.get("company") or "")

	tier_mode = str(params.get("tier") or "none")
	if tier_mode == "occupancy":
		occupied = occupancy.occupancy(key[0], key[1], today)["occupied"] if key[0] else True
		tier = "occupied" if occupied else "unoccupied"
	elif tier_mode.startswith("field:"):
		tier = str(anchor_row.get(tier_mode.split(":", 1)[1]) or "default")
	else:
		tier = "default"
	season = occupancy.in_season(company, today) if params.get("season") == "company" else True

	state = params.get("state") or {}
	knockdown = int(params.get("knockdown_days") or 0)
	in_knockdown = bool(knockdown) and _days(started[:10], anchor[:10]) < knockdown
	active = in_knockdown
	if not active and state.get("field"):
		value = str(anchor_row.get(state["field"]) or "")
		active = value in (state.get("active_values") or []) or (
			not value and state.get("blank_is_active", True)
		)
	if active:
		interval = int(params.get("active_interval_days") or 0)
		phase = "knockdown" if in_knockdown else "active"
	else:
		bands = params.get("intervals") or {}
		band = bands.get(tier) or bands.get("default") or {}
		interval = int(
			band.get("in_season_days" if season else "off_season_days")
			or params.get("active_interval_days")
			or 0
		)
		phase = "maintenance"
	due = (datetime.date.fromisoformat(anchor[:10]) + datetime.timedelta(days=interval)).isoformat()
	return {
		"location_doctype": key[0] or None,
		"location": key[1],
		"anchor_task": str(anchor_row["name"]),
		"anchor": anchor,
		"latest_placement": started,
		"tier": tier.capitalize() if tier in ("occupied", "unoccupied") else tier,
		"in_season": season,
		"phase": phase,
		"interval_days": interval,
		"due_date": due,
		"days_remaining": _days(today, due),
		"days_overdue": max(0, -_days(today, due)),
		"company": company,
	}


def scan(row: dict, context: dict, raw: dict) -> list:
	"""A declarative rule's scan when it carries `grouped_cadence`."""
	from .alerts.base import SEVERITY_CRITICAL, SEVERITY_WARNING, Observation
	from .alerts.engine import render_message

	today = str(context.get("today") or frappe.utils.today())[:10]
	company = str(context.get("company") or "")
	doctype = str(row.get("target_doctype") or "")
	warnings: list = []
	try:
		params = params_of(raw)
	except ValueError as exc:
		return [
			Observation(
				source_doctype=compliance_rules.DOCTYPE,
				source_docname=str(row.get("name") or row.get("rule_id")),
				message=f"This rule's grouped_cadence could not be read: {exc} Nothing it watches was checked.",
				severity=SEVERITY_WARNING,
				category="Records",
			)
		]
	if not compat.doctype_exists(doctype):
		return []
	template = str(row.get("message_template") or "")
	critical = str(row.get("severity_critical") or SEVERITY_CRITICAL)
	warning = str(row.get("severity_warning") or SEVERITY_WARNING)
	out = []
	for key, rows in groups(doctype, params, company).items():
		due = evaluate(rows, params, key, today)
		if not due or due["days_remaining"] > 0:
			continue
		overdue = due["days_overdue"]
		severity = critical if overdue > int(params.get("active_interval_days") or 0) else warning
		anchor_row = next(r for r in rows if str(r["name"]) == due["anchor_task"])
		message = (
			render_message(template, anchor_row, {**due, "today": today, "severity": severity}, warnings)
			if template
			else f"{key[1]}: due {due['due_date']} ({due['phase']}, every {due['interval_days']} days)."
		)
		out.append(
			Observation(
				source_doctype=doctype,
				source_docname=due["anchor_task"],
				message=message,
				severity=severity,
				due_date=due["due_date"],
				company=due["company"],
				category=str(row.get("category") or "") or "",
			)
		)
	return out

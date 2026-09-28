# SPDX-License-Identifier: MIT
"""What is due, on hours or on the calendar, and the job that raises the work.

A SERVICE SCHEDULE IS TWO INTERVALS AND EITHER ALONE IS COMPLETE. A tractor is
serviced every 250 hours, a fire extinguisher every 365 days, and an irrigation
pump on whichever of the two comes first. So `Asset Register` carries both
columns, both are optional, and an asset with neither is not on a schedule at
all — which is the right answer for most of a register and is reported as such
rather than as "not due".

WHICHEVER COMES FIRST, AND THE ANSWER SAYS WHICH. A machine that has run 260 of
its 250 hours and is 100 days into a 365-day interval is overdue on hours, and
telling somebody "due in 265 days" because the calendar was checked second is
how a schedule stops being believed. Both are computed, both are reported, and
`due_on` names the one that bit.

THE HOURS SIDE DEPENDS ON A METER READING AND SAYS SO WHEN IT HAS NONE. A
tractor with a 250-hour interval and nothing in its log is not overdue; it is
unmeasured, and `hours_due` comes back `None` with a reason. Reporting it as due
would fill a dispatch board with work nobody can verify, on the day an operator
first sets an interval — which is precisely when they are deciding whether to
keep this feature switched on.

WHAT `trigger_maintenance_tasks` WILL AND WILL NOT DO. It raises one Farm Task
per due asset through `create_farm_task`, which is the same door a foreman uses,
so the task carries an evidence contract and lands in the same board as
everything else. IT WILL NOT RAISE A SECOND ONE. An open service task against an
asset means the work is already on somebody's list, and a scheduled job that
re-raised it nightly would produce the exact backlog that teaches a crew to
ignore the board. That check is on the ASSET rather than on the interval, so a
task raised by hand suppresses the automatic one too.
"""

from __future__ import annotations

import frappe

from .. import compat, timezones
from ..args import as_bool, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import asset_tags, engine_hours

ASSET_REGISTER = asset_tags.ASSET_REGISTER
FARM_TASK = "Farm Task"

#: The task type a service job is raised as. `Repair` and not a new `Maintenance`
#: option: `Farm Task.task_type` is a Select with a fixed list, adding to it is a
#: schema change every stored filter and report column would have to learn, and a
#: scheduled service IS the repair bay's work. `as_choice` inside
#: `dispatch.create_farm_task` checks it, so a vocabulary change on the doctype
#: is caught there rather than silently here.
SERVICE_TASK_TYPE = "Repair"

#: What closing a service task obliges somebody to produce. A photograph of the
#: machine and what they found, in words — the two an insurer and a warranty
#: claim both ask for. Not a tick in a box; see `parse_evidence_required`.
SERVICE_EVIDENCE = {"photos": True, "findings_text": True}


def _live_states() -> tuple[str, ...]:
	"""The states that mean a service task is still work somebody owes.

	Read off the doctype's own vocabulary rather than listed here, so a state
	added to Farm Task cannot quietly start letting duplicate service jobs
	through the check below.
	"""
	from ..erpnext_mcp.doctype.farm_task.farm_task import STATES, TERMINAL_STATES

	return tuple(state for state in STATES if state not in TERMINAL_STATES)


#: Most assets one sweep will consider. A register larger than this is walked
#: over consecutive runs; the cap is REPORTED, because a silent one reads as
#: "nothing else is due".
SWEEP_CAP = 500

#: How close to the interval counts as "coming up" rather than merely "not due".
#: Ten percent of the interval, floored at a day and at an hour respectively:
#: a 250-hour service warns at 225 hours, and a 365-day one at 328 days. The
#: warning band exists so somebody can plan a service rather than discover it on
#: the morning a machine is needed.
WARN_FRACTION = 0.1

_SCHEDULE_FIELDS = (
	"name",
	"asset_type",
	"company",
	"location",
	"retired_at",
	"service_interval_hours",
	"service_interval_days",
	"last_service_date",
	"last_service_hours",
	"current_hours",
	"creation",
)


def _require() -> None:
	asset_tags._require()
	if not compat.has_field(ASSET_REGISTER, "service_interval_hours"):
		raise ToolError(
			"this site's Asset Register has no service schedule columns, so nothing can be due — "
			"run `bench --site <site> migrate` after upgrading the app."
		)


def _number(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def _days_between(later: str, earlier: str) -> int | None:
	if not later or not earlier:
		return None
	try:
		return int(frappe.utils.date_diff(later, earlier))
	except Exception:  # pragma: no cover - an unparseable stored date
		return None


def status_for(row: dict, today: str = "", hours_reading=None) -> dict:
	"""Is this asset due, on which interval, and by how much.

	`row` is an Asset Register record as a dict. `hours_reading` lets a caller
	that has ALREADY computed the machine's hours pass them in — the scan status
	report reads the whole engine-hours series anyway, and re-deriving it here
	would double the queries behind a screen a worker is waiting on.

	NEVER RAISES. It is called from a scan of anything, and a scan is not the
	place to discover that a register column will not cast to a float.
	"""
	today = today or str(frappe.utils.today())
	interval_hours = _number(row.get("service_interval_hours"))
	interval_days = int(_number(row.get("service_interval_days")))

	out: dict = {
		"asset_name": row.get("name"),
		"asset_type": row.get("asset_type") or None,
		"scheduled": bool(interval_hours or interval_days),
		"service_interval_hours": interval_hours or None,
		"service_interval_days": interval_days or None,
		"last_service_date": str(row.get("last_service_date") or "") or None,
		"last_service_hours": _number(row.get("last_service_hours")) or None,
		"due": False,
		"due_soon": False,
		"due_on": None,
		"hours_due": None,
		"hours_remaining": None,
		"hours_since_service": None,
		"current_hours": None,
		"days_due": None,
		"days_remaining": None,
		"days_since_service": None,
		"overdue_by_hours": None,
		"overdue_by_days": None,
		"next_service_date": None,
		"reasons": [],
		"message": "",
	}
	if not out["scheduled"]:
		out["message"] = (
			f"{row.get('name')} is not on a service schedule. Set service_interval_hours or "
			"service_interval_days on the asset to put it on one."
		)
		return out

	# ── the hours side ───────────────────────────────────────────────────────
	if interval_hours:
		current = hours_reading
		if current is None:
			current = _number(row.get("current_hours")) or None
		if current is None:
			out["reasons"].append(
				"An hours interval is set and no meter reading has ever been recorded on this "
				"machine, so hours cannot be measured. Enter engine_hours on the next check-out "
				"or check-in. NOT reported as due: unmeasured is not overdue."
			)
		else:
			since = round(current - _number(row.get("last_service_hours")), 1)
			out["current_hours"] = round(float(current), 1)
			out["hours_since_service"] = since
			out["hours_remaining"] = round(interval_hours - since, 1)
			out["hours_due"] = since >= interval_hours
			if out["hours_due"]:
				out["due"] = True
				out["due_on"] = "hours"
				out["overdue_by_hours"] = round(since - interval_hours, 1)
				out["reasons"].append(
					f"{since:g} h run since the last service against a {interval_hours:g} h "
					f"interval — {out['overdue_by_hours']:g} h over."
				)
			elif out["hours_remaining"] <= max(1.0, interval_hours * WARN_FRACTION):
				out["due_soon"] = True
				out["reasons"].append(f"{out['hours_remaining']:g} h until the next service.")

	# ── the calendar side ────────────────────────────────────────────────────
	if interval_days:
		anchor = str(row.get("last_service_date") or "")
		anchored_on = "the last service"
		if not anchor:
			anchor = str(row.get("creation") or "")[:10]
			anchored_on = "this asset's registration, because no service has been recorded"
		if anchor:
			elapsed = _days_between(today, anchor)
			if elapsed is not None:
				out["days_since_service"] = elapsed
				out["days_remaining"] = interval_days - elapsed
				out["days_due"] = elapsed >= interval_days
				try:
					out["next_service_date"] = str(frappe.utils.add_days(anchor, interval_days))
				except Exception:  # pragma: no cover
					out["next_service_date"] = None
				if out["days_due"]:
					out["due"] = True
					# WHICHEVER COMES FIRST, AND HOURS WIN A TIE. Where both
					# intervals have passed, `due_on` names hours — not because
					# ten hours over is "more" than five days over (the two
					# cannot be compared and pretending otherwise would be a
					# made-up ranking) but because a machine carrying an hours
					# interval is one whose wear somebody chose to measure in
					# hours, and that is the schedule the mechanic works to.
					# BOTH figures are reported either way; `due_on` is the
					# headline, not the whole answer.
					if out["due_on"] is None:
						out["due_on"] = "days"
					out["overdue_by_days"] = elapsed - interval_days
					out["reasons"].append(
						f"{elapsed} days since {anchored_on} against a {interval_days}-day "
						f"interval — {out['overdue_by_days']} days over."
					)
				elif out["days_remaining"] <= max(1, int(interval_days * WARN_FRACTION)):
					out["due_soon"] = True
					out["reasons"].append(f"{out['days_remaining']} days until the next service.")

	if out["due"]:
		last = out["last_service_date"] or "never"
		out["message"] = (
			f"OVERDUE — last service was {last}"
			+ (f", {out['days_since_service']} days ago" if out["days_since_service"] is not None else "")
			+ (
				f"; {out['hours_since_service']:g} h run since"
				if out["hours_since_service"] is not None
				else ""
			)
			+ "."
		)
	elif out["due_soon"]:
		parts = []
		if out["hours_remaining"] is not None and out["hours_remaining"] >= 0:
			parts.append(f"{out['hours_remaining']:g} hours")
		if out["days_remaining"] is not None and out["days_remaining"] >= 0:
			parts.append(f"{out['days_remaining']} days")
		out["message"] = "Service due in " + " / ".join(parts) + "." if parts else "Service due soon."
	else:
		parts = []
		if out["hours_remaining"] is not None:
			parts.append(f"{out['hours_remaining']:g} hours")
		if out["days_remaining"] is not None:
			parts.append(f"{out['days_remaining']} days")
		out["message"] = "Service due in " + " / ".join(parts) + "." if parts else "On schedule; nothing due."
	return out


def status_of(asset_name: str, hours_reading=None) -> dict:
	"""`status_for` from a docname, for callers holding one. Never raises."""
	try:
		row = asset_tags.asset_row(asset_name)
	except ToolError:  # pragma: no cover - a scan of something that vanished mid-call
		return {"asset_name": asset_name, "scheduled": False, "due": False, "reasons": [], "message": ""}
	return status_for(row, hours_reading=hours_reading)


# ── check_maintenance_due ───────────────────────────────────────────────────
def check_maintenance_due(args: dict) -> ToolResult:
	"""Is this asset due for service — or, with no asset named, which ones are."""
	_require()
	company = resolve_company(as_str(args, "company"))
	today = str(frappe.utils.today())
	clock = timezones.Renderer(args)

	named = as_str(args, "asset_name") or as_str(args, "asset")
	if named:
		row = asset_tags.asset_row(named, company or "")
		hours = None
		if engine_hours.has_hour_meter(str(row.get("asset_type") or "")):
			hours = engine_hours.summary_for(row["name"], args).get("current_hours")
		status = status_for(row, today, hours_reading=hours)
		data = {**status, "company": row.get("company") or None, "checked_on": today, **clock.block()}
		return ToolResult(
			data=data,
			summary=f"{row['name']}: " + (status["message"] or "on schedule"),
		)

	filters: dict = {"retired_at": ("is", "not set")}
	if company:
		filters["company"] = company
	asset_type = as_str(args, "asset_type")
	if asset_type:
		filters["asset_type"] = asset_type

	rows = (
		frappe.db.get_all(
			ASSET_REGISTER,
			filters=filters,
			fields=compat.existing_fields(ASSET_REGISTER, _SCHEDULE_FIELDS),
			order_by="name asc",
			limit=SWEEP_CAP + 1,
		)
		or []
	)
	truncated = len(rows) > SWEEP_CAP

	scheduled, due, soon = [], [], []
	for raw in rows[:SWEEP_CAP]:
		row = dict(raw)
		status = status_for(row, today)
		if not status["scheduled"]:
			continue
		scheduled.append(status)
		if status["due"]:
			due.append(status)
		elif status["due_soon"]:
			soon.append(status)

	return ToolResult(
		data={
			"company": company,
			"checked_on": today,
			"scheduled_count": len(scheduled),
			"due_count": len(due),
			"due_soon_count": len(soon),
			"due": due,
			"due_soon": soon,
			"scheduled": scheduled if as_bool(args, "include_all", False) else [],
			# A register walked to its cap is reported rather than presented as
			# the whole answer: "nothing else is due" and "I stopped looking" are
			# not the same sentence.
			"truncated": truncated,
			**clock.block(),
		},
		summary=(
			f"{len(due)} asset(s) due for service, {len(soon)} due soon, out of "
			f"{len(scheduled)} on a schedule"
			+ (f" for {company}" if company else "")
			+ (f" (register walked to the {SWEEP_CAP} cap)" if truncated else "")
		),
	)


# ── trigger_maintenance_tasks ───────────────────────────────────────────────
def _open_service_task(asset_name: str) -> str:
	"""An open service task already standing against this asset, or "".

	CHECKED ON THE ASSET, NOT ON THE INTERVAL. A service job raised by hand
	yesterday is the same work as the one this sweep would raise tonight, and a
	board with both on it is a board somebody stops reading.

	BOTH COLUMNS ARE CHECKED. A Farm Task reaches an asset through `asset` (which
	`report_asset_issue` writes) and through the dynamic `location` pair (which
	is what this module raises), and a duplicate check that looked at one of them
	would let a hand-raised repair and an automatic service stand side by side.
	"""
	if not compat.doctype_exists(FARM_TASK):
		return ""
	live = list(_live_states())
	for filters in (
		{"asset": asset_name},
		{"location_doctype": ASSET_REGISTER, "location": asset_name},
	):
		if any(not compat.has_field(FARM_TASK, column) for column in filters):
			continue
		try:
			rows = (
				frappe.db.get_all(
					FARM_TASK,
					filters={**filters, "task_type": SERVICE_TASK_TYPE, "state": ("in", live)},
					fields=["name"],
					order_by="creation desc",
					limit=1,
				)
				or []
			)
		except Exception:  # pragma: no cover - a site shaping these columns differently
			continue
		if rows:
			return str(rows[0]["name"])
	return ""


def trigger_maintenance_tasks(args: dict) -> ToolResult:
	"""Raise a Farm Task for every asset whose service is due. Skips duplicates.

	CALLABLE BY HAND AND FROM THE SCHEDULER, and it is the same code either way —
	see `sweep_due_maintenance` below for the bare entry point hooks.py names.
	A `dry_run` is the honest way to see what tonight would do before switching
	it on, and it is the default: this raises work for other people, and a tool
	whose first accidental call fills a dispatch board is one an operator turns
	off rather than tunes.
	"""
	_require()
	compat.require_doctype(
		FARM_TASK,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)
	company = resolve_company(as_str(args, "company"))
	dry_run = bool(as_bool(args, "dry_run", True))
	today = str(frappe.utils.today())
	limit = min(as_limit(args), SWEEP_CAP)

	filters: dict = {"retired_at": ("is", "not set")}
	if company:
		filters["company"] = company
	asset_type = as_str(args, "asset_type")
	if asset_type:
		filters["asset_type"] = asset_type
	named = as_str(args, "asset_name")
	if named:
		filters["name"] = named

	rows = (
		frappe.db.get_all(
			ASSET_REGISTER,
			filters=filters,
			fields=compat.existing_fields(ASSET_REGISTER, _SCHEDULE_FIELDS),
			order_by="name asc",
			limit=limit + 1,
		)
		or []
	)
	truncated = len(rows) > limit

	from . import dispatch

	created, skipped, failed, would_create = [], [], [], []
	for raw in rows[:limit]:
		row = dict(raw)
		status = status_for(row, today)
		if not status["due"]:
			continue

		existing = _open_service_task(row["name"])
		if existing:
			skipped.append(
				{
					"asset_name": row["name"],
					"reason": f"Farm Task {existing} is already open against this asset.",
					"task": existing,
				}
			)
			continue

		reason = " ".join(status["reasons"]) or status["message"]
		payload = {
			"task_name": f"Service {row['name']} ({status['due_on'] or 'schedule'})",
			"task_type": SERVICE_TASK_TYPE,
			"urgency": as_str(args, "urgency") or "Normal",
			"company": row.get("company") or company,
			"skill_required": asset_tags.ASSET_TYPE_SKILL_MAP.get(
				str(row.get("asset_type") or "General"), "general_maintenance"
			),
			"evidence_required": dict(SERVICE_EVIDENCE),
			# THE ASSET IS THE LOCATION, through the dynamic pair rather than
			# through `Farm Task.asset`: `create_farm_task` validates and writes
			# `location_doctype`/`location` and does not touch `asset` (that
			# column is `report_field_task`'s), and a task whose location a
			# dispatcher cannot resolve is one nobody can be routed to.
			"location_doctype": ASSET_REGISTER,
			"location": row["name"],
			"notes": (
				f"Raised automatically by the maintenance schedule on {today}. {reason} "
				"Record the service with record_service when it is done, so the next interval "
				"counts from the right place."
			),
		}

		if dry_run:
			would_create.append(
				{
					"asset_name": row["name"],
					"asset_type": row.get("asset_type"),
					"due_on": status["due_on"],
					"reason": reason,
					"task_name": payload["task_name"],
				}
			)
			continue

		try:
			result = dispatch.create_farm_task(payload)
			task = result.data or {}
			created.append(
				{
					"asset_name": row["name"],
					"task": task.get("name"),
					"due_on": status["due_on"],
					"reason": reason,
				}
			)
		except Exception as exc:  # pragma: no cover - reported, never raised
			# ONE ASSET THAT WILL NOT RAISE MUST NOT STOP THE SWEEP. This runs
			# nightly against a whole register; a single malformed row taking the
			# rest of the farm's services with it is the failure this app's other
			# scheduled jobs all refuse.
			failed.append({"asset_name": row["name"], "reason": f"{type(exc).__name__}: {exc}"})

	data = {
		"company": company,
		"checked_on": today,
		"dry_run": dry_run,
		"due_count": len(created) + len(skipped) + len(failed) + len(would_create),
		"created_count": len(created),
		"created": created,
		"would_create": would_create,
		"skipped_count": len(skipped),
		"skipped": skipped,
		"failed_count": len(failed),
		"failed": failed,
		"truncated": truncated,
	}
	if dry_run:
		data["note"] = (
			"DRY RUN — nothing was created. The list above is what a live call would raise. "
			"Send dry_run=false to raise it."
		)

	return ToolResult(
		data=data,
		summary=(
			(
				f"dry run: {len(would_create)} service task(s) would be raised"
				if dry_run
				else f"raised {len(created)} service task(s)"
			)
			+ (f", {len(skipped)} already open" if skipped else "")
			+ (f", {len(failed)} failed" if failed else "")
		),
		docstatus_delta="none → 0 (created)" if created else "",
	)


def sweep_due_maintenance() -> dict:
	"""Scheduler entry point — raise service tasks for everything that is due.

	BARE, LIVE AND ITERATING OVER EVERY COMPANY, which is the shape every other
	scheduled job in this app has: `hooks.py` names one dotted path and adding a
	company adds no line to it.

	NEVER RAISES. One company whose register will not walk must not stop the
	others, and a scheduler that throws is one an operator finds out about from a
	silent absence of work.
	"""
	report = {"companies": [], "created_count": 0, "failed": []}
	if not compat.doctype_exists(ASSET_REGISTER) or not compat.doctype_exists(FARM_TASK):
		return report
	try:
		companies = frappe.db.get_all("Company", pluck="name", limit=100) or []
	except Exception:  # pragma: no cover
		return report

	for company in companies:
		try:
			result = trigger_maintenance_tasks({"company": company, "dry_run": False})
			data = result.data or {}
			report["companies"].append(
				{
					"company": str(company),
					"created_count": data.get("created_count", 0),
					"skipped_count": data.get("skipped_count", 0),
				}
			)
			report["created_count"] += int(data.get("created_count") or 0)
		except Exception as exc:  # pragma: no cover - reported, never raised
			report["failed"].append({"company": str(company), "reason": f"{type(exc).__name__}: {exc}"})
	return report

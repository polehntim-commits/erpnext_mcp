# SPDX-License-Identifier: MIT
"""The one screen the person who owns the farm actually opens.

WHAT WAS ALREADY HERE AND WHY IT WAS NOT THIS. Every number on this dashboard
existed and each lived behind its own call. Crews are in `list_shifts`, harvest
in the bucket sessions, compliance in `get_audit_readiness`, money in
`compute_all_kpis`, weather on the shift's own readings, work waiting on somebody
in `list_pending_approvals` and `list_dispatch_board`. Seven calls, seven shapes,
and no answer to the only question an owner asks at six in the morning, which is
**is anything wrong today**.

Assembling that by hand is a habit nobody keeps. So this is one call, and the
part that makes it a dashboard rather than a dump is `attention`.

────────────────────────────────────────────────────────────────────────────
`attention` IS THE PRODUCT. EVERYTHING ELSE IS ITS EVIDENCE
────────────────────────────────────────────────────────────────────────────

A ranked list of what is wrong right now, each row naming the tool that answers
it in full. Ranked by SEVERITY and not by section, because an open Critical
compliance alert and a KPI two percent off target are not two items on one list —
and a dashboard that presented them as though they were would train somebody to
stop reading it.

Nothing invents a threshold. Every severity here is read off the record that
raised it: a Compliance Alert's own severity, a KPI's own thresholds, a shift's
own company heat thresholds. This module decides ORDER, never gravity.

────────────────────────────────────────────────────────────────────────────
A SOURCE THAT REFUSES IS REPORTED, NEVER FATAL
────────────────────────────────────────────────────────────────────────────

`disclosure.py` made this argument for the MD&A feed and it is stronger here: a
dashboard that raises because the caller lacks the shift role, or because a farm
mid-setup has no KPI definitions, is a dashboard nobody opens twice. Every source
runs through `_try`, and a source that refuses lands in `unavailable` with the
reason it gave.

**AN UNAVAILABLE SOURCE IS NOT A CLEAN ONE.** `sections_reporting` and
`sections_unavailable` are both returned, and the summary says how many of each.
A dashboard showing no compliance alerts because the compliance feed refused
looks exactly like a farm with no compliance alerts, and that confusion is the
one this read must never produce.

────────────────────────────────────────────────────────────────────────────
WEATHER COMES OFF THE SHIFT, NOT OFF THE INTERNET
────────────────────────────────────────────────────────────────────────────

`fetch_weather_now` exists and is deliberately not called here. It writes a
reading, it refuses a closed shift, and a dashboard that reached Open-Meteo on
every render would put an outbound HTTP call on the path of a screen somebody
refreshes. The scheduled sweep already collects readings every fifteen minutes
onto the open shifts; this reports the most recent of them, with its own
timestamp and `source` so an hour-old reading cannot be read as a live one.
"""

from __future__ import annotations

import frappe

from .. import compat, dashboard
from .. import shifts as shift_engine
from ..args import as_date, as_int, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult

#: How many rows any one section carries. A dashboard is read at a glance; the
#: tool named on each row is where the whole list lives.
PREVIEW = 10

#: Ranking for `attention`, worst first. Read off the records themselves —
#: this module decides ORDER, never gravity.
SEVERITY_ORDER = ("Critical", "Warning", "Info")


def _try(label: str, unavailable: list, call, *args, **kwargs):
	"""Run one source. A source that fails is REPORTED, never fatal.

	The same helper `disclosure.py` uses, for the same reason and one more: this
	dashboard composes tools that each enforce their own role, so a caller
	holding some of those roles gets the sections they may see and a named
	refusal for the rest, rather than an error page.
	"""
	try:
		result = call(*args, **kwargs)
		return result.data if isinstance(result, ToolResult) else result
	except ToolError as exc:
		unavailable.append({"section": label, "reason": str(exc), "kind": "refused"})
	except Exception as exc:  # pragma: no cover - a site missing a doctype entirely
		unavailable.append({"section": label, "reason": f"{type(exc).__name__}: {exc}", "kind": "error"})
	return None


def _item(severity: str, section: str, headline: str, detail: str, tool: str, count=None) -> dict:
	return {
		"severity": severity,
		"section": section,
		"headline": headline,
		"detail": detail,
		"count": count,
		"read_it_with": tool,
	}


def get_owner_dashboard(args: dict) -> ToolResult:
	"""Crews, harvest, compliance, money, weather and what is waiting — in one call."""
	company = resolve_company(as_str(args, "company"), required=True)
	as_of = as_date(args, "as_of") or str(frappe.utils.today())
	preview = max(1, min(as_int(args, "preview", PREVIEW), 50))

	unavailable: list = []
	sections: dict = {}
	attention: list = []

	_crews(company, as_of, preview, sections, attention, unavailable)
	_harvest(company, as_of, preview, sections, attention, unavailable)
	_compliance(company, as_of, preview, sections, attention, unavailable)
	_camp(company, sections, attention, unavailable)
	_money(company, as_of, sections, attention, unavailable)
	_weather(sections, attention)
	_waiting(company, preview, sections, attention, unavailable)

	attention.sort(
		key=lambda row: (
			SEVERITY_ORDER.index(row["severity"]) if row["severity"] in SEVERITY_ORDER else 9,
			row["section"],
		)
	)
	worst = attention[0]["severity"] if attention else None
	critical = len([row for row in attention if row["severity"] == "Critical"])

	return ToolResult(
		data={
			"company": company,
			"as_of": as_of,
			"attention": attention,
			"attention_count": len(attention),
			"critical_count": critical,
			"worst_severity": worst,
			"all_clear": not attention,
			**sections,
			"sections_reporting": sorted(sections),
			"sections_unavailable": sorted(entry["section"] for entry in unavailable),
			"unavailable": unavailable,
			"note": (
				"`attention` is the product and everything else is its evidence — ranked by the "
				"severity each record carries rather than by section, because an open Critical "
				"and a KPI two percent off target are not two items on one list. "
				"`sections_unavailable` matters just as much: a dashboard showing no compliance "
				"alerts because the compliance source refused looks exactly like a farm with "
				"none, and those are not the same farm."
			),
		},
		summary=(
			f"{company} on {as_of}: "
			+ (
				f"{len(attention)} item(s) need attention, {critical} critical"
				if attention
				else "nothing needs attention"
			)
			+ f" ({len(sections)} section(s) reporting"
			+ (f", {len(unavailable)} unavailable" if unavailable else "")
			+ ")"
		),
	)


# ── who is on the ground ────────────────────────────────────────────────────
def _crews(company: str, as_of: str, preview: int, sections, attention, unavailable) -> None:
	from . import shifts as shift_tools

	data = _try(
		"crews",
		unavailable,
		shift_tools.list_shifts,
		{"company": company, "from_date": as_of, "to_date": as_of, "limit": 200},
	)
	if data is None:
		return
	rows = data.get("shifts") or []
	open_rows = [row for row in rows if row.get("open")]
	on_the_clock = 0
	for row in open_rows:
		crew = shift_engine.crew_of(str(row.get("name") or ""))
		row["crew_size"] = len(crew)
		row["still_on_shift"] = len([entry for entry in crew if not entry.get("left_at")])
		on_the_clock += row["still_on_shift"]

	sections["crews"] = {
		"shifts_today": len(rows),
		"open_shifts": len(open_rows),
		"people_on_the_clock": on_the_clock,
		"foremen": sorted(
			{str(row.get("foreman_name") or row.get("foreman") or "") for row in open_rows} - {""}
		),
		"shifts": [
			{
				"shift": row.get("name"),
				"foreman": row.get("foreman_name") or row.get("foreman"),
				"location": row.get("location"),
				"started": row.get("start_datetime"),
				"crew_size": row.get("crew_size"),
				"still_on_shift": row.get("still_on_shift"),
			}
			for row in open_rows[:preview]
		],
		"read_it_with": "list_shifts",
	}

	# AN OPEN SHIFT FROM A PREVIOUS DAY IS THE ONE THING WORTH RAISING HERE. A
	# crew that never clocked out has no Attendance for the day, which is a wage
	# record that does not exist rather than one that is wrong.
	stale = [
		row["shift"]
		for row in sections["crews"]["shifts"]
		if str(row.get("started") or "")[:10] and str(row["started"])[:10] < as_of
	]
	if stale:
		attention.append(
			_item(
				"Warning",
				"crews",
				f"{len(stale)} shift(s) opened before today are still open",
				"end_shift writes one submitted Attendance per crew member, so a shift nobody "
				"closed is a day of wages with no record behind it — not a record that is wrong, "
				"one that does not exist. " + ", ".join(stale[:5]),
				"list_shifts",
				len(stale),
			)
		)


# ── what came off the trees ─────────────────────────────────────────────────
def _harvest(company: str, as_of: str, preview: int, sections, attention, unavailable) -> None:
	from . import bucket_log

	data = _try(
		"harvest",
		unavailable,
		bucket_log.list_bucket_entries,
		{"company": company, "from_date": as_of, "to_date": as_of, "limit": 2000},
	)
	if data is None:
		return
	entries = [dict(row) for row in data.get("entries") or []]
	accepted = [row for row in entries if str(row.get("verdict") or "") == "Accepted"]
	rejected = len(entries) - len(accepted)
	pickers = sorted({str(row.get("employee") or row.get("worker_badge") or "") for row in entries} - {""})

	sections["harvest"] = {
		"captures_today": len(entries),
		"accepted": len(accepted),
		"rejected": rejected,
		"reject_rate_percent": round(100.0 * rejected / len(entries), 1) if entries else None,
		"pickers_recorded": len(pickers),
		"unlinked_to_payroll": len([row for row in entries if str(row.get("status") or "") == "Pending"]),
		"truncated": bool(data.get("truncated")),
		"read_it_with": "list_bucket_entries",
	}

	unresolved = len([row for row in entries if not str(row.get("employee") or "").strip()])
	if unresolved:
		attention.append(
			_item(
				"Warning",
				"harvest",
				f"{unresolved} capture(s) today have no employee resolved",
				"The badge was scanned and this site cannot say whose it is, so those buckets pay "
				"nobody. link_badge_to_employee backfills them onto the captures already synced.",
				"list_bucket_entries",
				unresolved,
			)
		)


# ── the compliance posture ──────────────────────────────────────────────────
def _compliance(company: str, as_of: str, preview: int, sections, attention, unavailable) -> None:
	from . import calendar as calendar_tools

	score = _try(
		"compliance",
		unavailable,
		calendar_tools.get_audit_readiness,
		{"company": company, "as_of": as_of},
	)
	if score is None:
		return
	by_severity = dict(score.get("by_severity") or {})
	sections["compliance"] = {
		"audit_readiness_score": score.get("audit_readiness_score"),
		"raised": score.get("raised"),
		"resolved": score.get("resolved"),
		"open": score.get("open"),
		"snoozed": score.get("snoozed"),
		"by_severity": by_severity,
		"resolved_by_hand_percent": score.get("resolved_by_hand_percent"),
		"warnings": score.get("warnings") or [],
		"read_it_with": "get_audit_readiness",
	}

	# THE SEVERITY IS THE ALERT'S OWN. A Critical is critical because the rule
	# that raised it said so, not because this module ranked it.
	for severity in SEVERITY_ORDER:
		count = int(by_severity.get(severity) or 0)
		if not count or severity == "Info":
			continue
		attention.append(
			_item(
				severity,
				"compliance",
				f"{count} open {severity} compliance alert(s)",
				"get_compliance_calendar names each one and what makes it true. A single open "
				"Critical can sit under a high readiness score — the score is a ratio and "
				"something has stopped being lawful whatever the percentage says.",
				"get_compliance_calendar",
				count,
			)
		)

	packets = _try(
		"audit_packets",
		unavailable,
		_policy_gaps,
		company,
		as_of,
	)
	if packets:
		sections["sop_coverage"] = packets
		if packets.get("categories_with_no_policy"):
			attention.append(
				# ITS OWN SECTION, not "compliance". The section on an attention row
				# is the drill-down it belongs to, and this one drills into
				# get_policy_coverage rather than into the alert calendar — filing
				# it under compliance would send somebody to the wrong screen.
				_item(
					"Warning",
					"sop_coverage",
					f"{len(packets['categories_with_no_policy'])} SOP category/categories have no policy in force",
					"Each is a section an audit packet produces short: "
					+ ", ".join(packets["categories_with_no_policy"][:6]),
					"get_policy_coverage",
					len(packets["categories_with_no_policy"]),
				)
			)


def _policy_gaps(company: str, as_of: str) -> dict:
	from . import evidence

	data = evidence.get_policy_coverage({"company": company, "as_of": as_of}).data
	return {
		"categories_with_no_policy": data.get("categories_with_no_policy") or [],
		"policies_without_a_document": data.get("policies_without_a_document") or [],
		"read_it_with": "get_policy_coverage",
	}


# ── the camp ────────────────────────────────────────────────────────────────
def _camp(company: str, sections, attention, unavailable) -> None:
	from . import housing

	data = _try("camp", unavailable, housing.get_housing_capacity, {"company": company})
	if data is None:
		return
	sections["camp"] = {
		"unit_count": data.get("unit_count"),
		"total_capacity": data.get("total_capacity"),
		"currently_assigned": data.get("currently_assigned"),
		"open_beds": data.get("open_beds"),
		"overdue_inspection_count": data.get("overdue_inspection_count"),
		"overdue_detector_test_count": data.get("overdue_detector_test_count"),
		"read_it_with": "get_housing_capacity",
	}
	# The two camp backlogs are raised SEPARATELY. They are different errands
	# with different skills and different evidence, and one number covering both
	# is a number nobody can plan a morning from.
	for count, label, tool in (
		(data.get("overdue_inspection_count") or 0, "habitability inspection", "list_housing_units"),
		(data.get("overdue_detector_test_count") or 0, "detector test", "list_housing_units"),
	):
		if count:
			attention.append(
				_item(
					"Warning",
					"camp",
					f"{count} unit(s) overdue for a {label}",
					f"list_housing_units names them under overdue_{'inspections' if 'habit' in label else 'detector_tests'}. "
					"Somebody sleeps there tonight.",
					tool,
					count,
				)
			)


# ── the money ───────────────────────────────────────────────────────────────
def _money(company: str, as_of: str, sections, attention, unavailable) -> None:
	from . import kpidefs

	start = str(frappe.utils.add_days(as_of, -90))
	data = _try(
		"financial_kpis",
		unavailable,
		kpidefs.compute_all_kpis,
		{"company": company, "period_start": start, "period_end": as_of},
	)
	if data is None:
		return
	rows = [dict(row) for row in data.get("kpis") or []]
	breached = [dict(row) for row in data.get("breached") or []]

	sections["financial"] = {
		"window": {"from": start, "to": as_of},
		"kpi_count": data.get("kpi_count"),
		"breach_count": data.get("breach_count"),
		"kpis": [
			{
				"kpi": (row.get("definition") or {}).get("kpi_id") or row.get("kpi_key"),
				"title": (row.get("definition") or {}).get("title"),
				"value": row.get("value"),
				"unit": (row.get("definition") or {}).get("unit"),
				"status": (row.get("threshold_status") or {}).get("status"),
			}
			for row in rows
		],
		# AN EMPTY `breached` IS NOT A HEALTHY OPERATION, which is compute_all_kpis'
		# own warning and is repeated here rather than dropped: a KPI with no
		# thresholds can never appear there whatever it is worth.
		"unwatched_note": data.get("unwatched_note"),
		"read_it_with": "compute_all_kpis",
	}

	# THE THRESHOLD IS THE KPI DEFINITION'S OWN, editable with
	# update_financial_kpi_definition and needing no code release. Nothing here
	# decides what "bad" is for a farm it has never seen — including the
	# severity, which is the status the definition's own bands produced.
	for row in breached:
		status = str(row.get("status") or "")
		attention.append(
			_item(
				"Critical" if "critical" in status.lower() else "Warning",
				"financial",
				f"{row.get('title') or row.get('kpi_id')} is past its own {status or 'threshold'}",
				str(row.get("message") or "")
				or "The thresholds are on the KPI definition and editable with "
				"update_financial_kpi_definition.",
				"compute_all_kpis",
				None,
			)
		)


# ── the weather the crew is standing in ─────────────────────────────────────
def _weather(sections, attention) -> None:
	"""The latest reading on each open shift. NOT a live fetch — see the docstring."""
	crews = sections.get("crews")
	if not crews:
		return
	readings = []
	for row in crews.get("shifts") or []:
		found = shift_engine.weather_of(str(row.get("shift") or ""))
		if not found:
			continue
		latest = found[-1]
		readings.append(
			{
				"shift": row.get("shift"),
				"location": row.get("location"),
				"reading_datetime": str(latest.get("reading_datetime") or "") or None,
				"temp_f": latest.get("temp_f"),
				"heat_index_f": latest.get("heat_index_f"),
				"humidity_pct": latest.get("humidity_pct"),
				"wind_speed_mph": latest.get("wind_speed_mph"),
				# WHERE THE NUMBER CAME FROM. A live fifteen-minute reading and one
				# reconstructed from the hourly archive are different kinds of
				# evidence, and this column is what stops the second reading as
				# the first.
				"source": latest.get("source"),
			}
		)
	silent = [
		row.get("shift")
		for row in crews.get("shifts") or []
		if row.get("shift") not in {entry["shift"] for entry in readings}
	]
	sections["weather"] = {
		"open_shifts_with_a_reading": len(readings),
		"open_shifts_with_no_reading": silent,
		"readings": readings,
		"read_it_with": "get_weather_timeline",
		"note": (
			"The most recent reading the scheduled sweep collected onto each open shift, with "
			"its own timestamp. Nothing here calls out to a weather service: a dashboard that "
			"reached the internet on every refresh would put an outbound request on the path of "
			"a screen somebody leaves open. fetch_weather_now is the deliberate call for the "
			"moment the schedule is too slow for."
		),
	}
	if silent:
		attention.append(
			_item(
				"Warning",
				"weather",
				f"{len(silent)} open shift(s) have no weather reading at all",
				"OAR 437-004-1131 obligations are triggered by a heat index nobody is measuring "
				"on these shifts. " + ", ".join(str(name) for name in silent[:5]),
				"list_shifts_missing_weather",
				len(silent),
			)
		)


# ── what is waiting on somebody ─────────────────────────────────────────────
def _waiting(company: str, preview: int, sections, attention, unavailable) -> None:
	from . import dispatch, workflow

	approvals = _try("approvals", unavailable, workflow.list_pending_approvals, {"limit": 200})
	if approvals is not None:
		# `pending` is one row per WORKFLOW STATE, not per document, and
		# `document_count` is the number a person cares about. Counting the
		# groups would report four states as four documents.
		groups = [dict(row) for row in approvals.get("pending") or []]
		waiting = int(approvals.get("document_count") or 0)
		sections["approvals"] = {
			"document_count": waiting,
			"state_count": len(groups),
			"by_state": [
				{
					"doctype": row.get("doctype"),
					"state": row.get("state"),
					"count": row.get("count"),
					"allowed_roles": row.get("allowed_roles"),
				}
				for row in groups[:preview]
			],
			"read_it_with": "list_pending_approvals",
		}
		if waiting:
			attention.append(
				_item(
					"Info",
					"approvals",
					f"{waiting} document(s) parked awaiting an approval",
					"list_pending_approvals with a `user` narrows it to the states that person's "
					"roles can actually act on, which is the worklist question people mean.",
					"list_pending_approvals",
					waiting,
				)
			)

	board = _try(
		"dispatch",
		unavailable,
		dispatch.list_dispatch_board,
		{"company": company, "limit": 200},
	)
	if board is not None:
		pool = list(board.get("in_the_pool") or [])
		critical = list(board.get("open_critical") or [])
		sections["dispatch"] = {
			"open_tasks": board.get("count"),
			"by_state": board.get("by_state"),
			"in_the_pool": len(pool),
			"open_critical": len(critical),
			"from_a_compliance_alert": board.get("generated_from_alerts"),
			"not_anchored_to_a_shift": len(board.get("not_anchored_to_a_shift") or []),
			"kanban_route": board.get("kanban_route"),
			"read_it_with": "list_dispatch_board",
		}
		if critical:
			attention.append(
				_item(
					"Critical",
					"dispatch",
					f"{len(critical)} open Critical task(s) on the dispatch board",
					"Each was raised by a rule that is still true, so the alert behind it has not "
					"gone away either. " + ", ".join(str(name) for name in critical[:5]),
					"list_dispatch_board",
					len(critical),
				)
			)
		if pool:
			attention.append(
				_item(
					"Info",
					"dispatch",
					f"{len(pool)} task(s) are in the pool with nobody on them",
					"Self-pick work nobody has picked. Fifty-four habitability walks a foreman "
					"has to assign by hand are fifty-four walks that do not happen — but a pool "
					"nobody draws from is the same outcome by a different route.",
					"list_dispatch_board",
					len(pool),
				)
			)


def command_center_installed() -> bool:
	"""Whether the Desk dashboard this read complements is on the site."""
	return compat.doctype_exists(dashboard.DASHBOARD)

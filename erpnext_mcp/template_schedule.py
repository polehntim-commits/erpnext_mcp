# SPDX-License-Identifier: MIT
"""Templates that raise their own tasks — on a date, a repeat, a crop stage or degree days. v0.278.0.

THE TIMING IS DATA ON THE TEMPLATE (`task_templates.PLAN_FIELDS`), not a compliance rule per job: `schedule` (Once a
year / Daily / Weekly / Monthly, every N, a weekday), a season window (MM-DD to MM-DD, may wrap the new year), and
for one task per block of a crop, the block's recorded BBCH stage and its growing degree days (°F·day since the
biofix, the same number `get_degree_days` gives). One daily run raises what is due, once per period and place —
the period key is in `source_workorder`, so a second run that day writes nothing.

OFF per company until `scheduled_templates_enabled` is on for it. A template with no schedule is never touched.
A block with no recorded stage is not raised by a stage-gated template, and says so in the run's report — a stage
is something somebody saw, not something guessed.
"""

from __future__ import annotations

import datetime

import frappe

from . import compat

FLAG = "scheduled_templates_enabled"
TEMPLATE = "Farm Task Template"
TASK = "Farm Task"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _in_window(day: datetime.date, start: str, end: str) -> bool:
	if not start or not end:
		return True
	mmdd = day.strftime("%m-%d")
	return start <= mmdd <= end if start <= end else (mmdd >= start or mmdd <= end)


def period(row: dict, day: datetime.date) -> str | None:
	"""The period key for `day`, or None when the template is not due that day."""
	schedule = row.get("schedule") or ""
	every = max(1, int(row.get("schedule_every") or 1))
	if not schedule or not _in_window(day, row.get("window_start") or "", row.get("window_end") or ""):
		return None
	if schedule == "Once a year":
		return f"{day.year}"
	if schedule == "Daily":
		return day.isoformat() if day.timetuple().tm_yday % every == 0 or every == 1 else None
	if schedule == "Weekly":
		weekday = row.get("schedule_weekday") or ""
		if weekday and WEEKDAYS[day.weekday()] != weekday:
			return None
		year, week, _ = day.isocalendar()
		if not weekday and day.weekday() != 0:
			return None  # a weekly template with no weekday raises on Monday
		return f"{year}-W{week:02d}" if week % every == 0 or every == 1 else None
	if schedule == "Monthly":
		return f"{day.year}-{day.month:02d}" if (day.month - 1) % every == 0 else None
	return None


def _blocks(company: str, crop: str) -> list:
	if not compat.doctype_exists("Field"):
		return []
	wanted = str(crop or "").strip().casefold()
	rows = frappe.db.get_all("Field", filters={"owning_entity": company}, fields=["name", "crop"], limit=5000) or []
	return [r["name"] for r in rows if wanted and wanted in str(r.get("crop") or "").casefold()]


def _gate(row: dict, block: str, day: datetime.date) -> tuple:
	"""(due, reason) for one block of a stage / degree-day gated template."""
	from . import bbch, growth_stage

	if row.get("bbch_from") not in (None, "") or row.get("bbch_to") not in (None, ""):
		last = growth_stage.latest(block, day.year)
		code = bbch.parse((last or {}).get("growth_stage_code"))
		if code is None:
			return False, "no crop stage recorded this season"
		if row.get("bbch_from") not in (None, "") and code < int(row["bbch_from"]):
			return False, f"stage {code} is before {row['bbch_from']}"
		if row.get("bbch_to") not in (None, "") and code > int(row["bbch_to"]):
			return False, f"stage {code} is past {row['bbch_to']}"
	if row.get("dd_from") not in (None, "") or row.get("dd_to") not in (None, ""):
		from . import degree_days

		try:
			gdd = degree_days.for_block(block, as_of=day.isoformat())
		except Exception:
			gdd = {}
		if not gdd.get("available"):
			return False, gdd.get("reason") or "no degree days for this block (Weather Settings → Forecast Enabled?)"
		total = float(gdd.get("gdd_season") or 0)
		if row.get("dd_from") not in (None, "") and total < float(row["dd_from"]):
			return False, f"{total:g} °F·day, before {float(row['dd_from']):g}"
		if row.get("dd_to") not in (None, "") and total > float(row["dd_to"]):
			return False, f"{total:g} °F·day, past {float(row['dd_to']):g}"
	return True, ""


def due(company: str, day: datetime.date | None = None) -> list:
	"""What the scheduled templates would raise for a company today: [{template, place, key, due, reason}]."""
	from . import task_templates

	day = day or datetime.date.fromisoformat(str(frappe.utils.today())[:10])
	if not compat.has_field(TEMPLATE, "schedule"):
		return []
	fields = compat.existing_fields(TEMPLATE, ("name", "template_name", "company", "enabled", *task_templates.PLAN_FIELDS))
	out = []
	for row in frappe.db.get_all(TEMPLATE, filters={"schedule": ("is", "set")}, fields=fields, limit=2000) or []:
		row = dict(row)
		if not compat.checked(row.get("enabled")) or (row.get("company") and row["company"] != company):
			continue
		key = period(row, day)
		if key is None:
			continue
		if row.get("schedule_target") == "Each block of the crop":
			for block in _blocks(company, row.get("schedule_crop")):
				ok, why = _gate(row, block, day)
				out.append({"template": row["name"], "template_name": row.get("template_name"), "place": block,
				            "key": f"schedule:{row['name']}:{key}:{block}"[:140], "due": ok, "reason": why or None})
		else:
			out.append({"template": row["name"], "template_name": row.get("template_name"), "place": None,
			            "key": f"schedule:{row['name']}:{key}:{company}"[:140], "due": True, "reason": None})
	return out


def run(company: str, day: datetime.date | None = None) -> dict:
	"""Raise what is due and not already raised for this period and place."""
	from .tools import tasktemplates

	raised, already, waiting, refused = [], [], [], []
	for item in due(company, day):
		if not item["due"]:
			waiting.append(item)
			continue
		if frappe.db.exists(TASK, {"source_workorder": item["key"]}):
			already.append(item["key"])
			continue
		args = {"template": item["template"], "company": company}
		if item["place"]:
			args.update({"location_doctype": "Field", "location": item["place"]})
		try:
			result = tasktemplates.create_task_from_template(args, origin="compliance_rule",
			                                                 fields={"source_workorder": item["key"]})
			raised.append((result.data or {}).get("name"))
		except Exception as exc:  # one template's problem never stops the others
			refused.append({**item, "reason": str(exc)[:300]})
	return {"company": company, "raised": raised, "already": already, "waiting": waiting, "refused": refused}


def daily() -> dict:
	from . import flags

	out = {}
	for company in [r["name"] for r in frappe.db.get_all("Company", fields=["name"], limit=200) or []]:
		if flags.value(FLAG, company=company, default=False):
			try:
				out[company] = run(company)
			except Exception:
				frappe.log_error(title="erpnext_mcp: scheduled templates", message=frappe.get_traceback())
	return out

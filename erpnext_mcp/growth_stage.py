# SPDX-License-Identifier: MIT
"""Crop stage: capture and timeline. v0.241.0. docs/design/ccf_core_work_timing.md §3 (approved; queue item 5).

A STAGE IS A CROP OBSERVATION of type "Growth Stage" — no new doctype. Its BBCH code is what the
Go / Hold rules read (`phenology.bbch`, v0.239.0).

* BBCH IS REQUIRED ON THE PHONE (the picker will not save without one) AND FLAGGED OVER MCP
  (decision 25): a stage recorded here with words but no code, or a code that does not parse, is kept
  and flagged — the words are still a fact somebody saw.
* BACKWARDS IS FLAGGED, NOT BLOCKED (decision 28): a code earlier than the block's last one this year
  is kept with a flag naming the earlier record. Somebody may have mis-tapped either one.
* PROMPTS (decision 27): the start of a task, an issue report, and the phone's End of Day ask for the
  stage of a block when none is on record this year or the last is more than STALE_DAYS old. A prompt
  is a question, never a refusal.
"""

from __future__ import annotations

import datetime

import frappe

from . import bbch, compat

DOCTYPE = "Crop Observation"
TYPE = "Growth Stage"
STALE_DAYS = 7


def installed() -> bool:
	return compat.has_field(DOCTYPE, "stage_flags")


def _date(value) -> datetime.date | None:
	try:
		return datetime.date.fromisoformat(str(value or "")[:10])
	except ValueError:
		return None


def _today() -> datetime.date:
	return _date(frappe.utils.today()) or datetime.date.today()


def stages(block: str, year: int | None = None, limit: int = 500) -> list:
	"""Every stage observation on a block (any observation with a code or a stage in words), oldest first."""
	fields = compat.existing_fields(
		DOCTYPE,
		("name", "observation_type", "block_doctype", "block", "observed_on", "observed_at", "observer",
		 "growth_stage_code", "crop_stage", "crop", "stage_flags", "source_task", "observed_gps", "photo", "notes"),
	)
	rows = frappe.db.get_all(DOCTYPE, filters={"block": block}, fields=fields, limit=5000) or []
	out = []
	for row in rows:
		row = dict(row)
		if not (str(row.get("growth_stage_code") or "").strip() or row.get("observation_type") == TYPE):
			continue
		day = _date(row.get("observed_at")) or _date(row.get("observed_on"))
		if year and (not day or day.year != int(year)):
			continue
		row["_day"] = day
		out.append(row)
	out.sort(key=lambda r: (str(r.get("observed_at") or r.get("observed_on") or ""), str(r.get("name"))))
	return out[-limit:]


def latest(block: str, year: int | None = None) -> dict | None:
	"""The last observation with a parseable code."""
	for row in reversed(stages(block, year)):
		if bbch.parse(row.get("growth_stage_code")) is not None:
			return row
	return None


def flags_for(block: str, code: str, words: str, on: datetime.date) -> list:
	flags = []
	number = bbch.parse(code)
	if not str(code or "").strip():
		flags.append("No BBCH code — " + (f"recorded as “{words}”; " if words else "") + "verify the stage.")
	elif number is None:
		flags.append(f"“{code}” is not a BBCH code (00–99) — verify the stage.")
	if number is not None:
		last = latest(block, on.year)
		last_number = bbch.parse((last or {}).get("growth_stage_code"))
		if last_number is not None and number < last_number and (last.get("_day") or on) <= on:
			flags.append(
				f"Earlier than BBCH {bbch.normalise(last['growth_stage_code'])} recorded "
				f"{last.get('_day') or ''} ({last.get('name')}) — check which is right."
			)
	return flags


def record(
	*,
	block: str,
	block_doctype: str = "Field",
	code: str = "",
	words: str = "",
	observed_at: str = "",
	observer: str = "",
	company: str = "",
	crop: str = "",
	source_task: str = "",
	gps: str = "",
	photo: str = "",
	notes: str = "",
) -> dict:
	"""File one stage. Never refused for its code; flagged instead (decisions 25, 28)."""
	if not str(code or "").strip() and not str(words or "").strip():
		raise ValueError("give the BBCH code (e.g. 55) or, failing that, the stage in words.")
	when = str(observed_at or frappe.utils.now())
	on = _date(when) or _today()
	if on > _today():
		raise ValueError(f"observed_at {when} is in the future.")
	flags = flags_for(block, code, words, on)
	doc = frappe.new_doc(DOCTYPE)
	doc.update(
		{
			"observation_type": TYPE,
			"company": company or None,
			"block_doctype": block_doctype or "Field",
			"block": block,
			"observed_on": on.isoformat(),
			"observed_at": when,
			"observer": observer or None,
			"growth_stage_code": bbch.normalise(code) if bbch.parse(code) is not None else (str(code or "").strip() or None),
			"crop_stage": str(words or "").strip() or (bbch.describe(code) if bbch.parse(code) is not None else None),
			"crop": crop or None,
			"stage_flags": "\n".join(flags) or None,
			"source_task": source_task or None,
			"observed_gps": gps or None,
			"photo": photo or None,
			"notes": notes or None,
		}
	)
	doc.insert(ignore_permissions=True)
	return {"observation": doc.name, "block": block, "bbch": doc.growth_stage_code, "stage": doc.crop_stage,
	        "flags": flags, "observed_at": when}


def timeline(block: str, year: int | None = None) -> dict:
	year = int(year or _today().year)
	rows = stages(block, year)
	entries = []
	for row in rows:
		entries.append(
			{
				"observation": row["name"],
				"date": row["_day"].isoformat() if row.get("_day") else None,
				"observed_at": str(row.get("observed_at") or "") or None,
				"bbch": row.get("growth_stage_code") or None,
				"stage": row.get("crop_stage") or (bbch.describe(row.get("growth_stage_code")) if row.get("growth_stage_code") else None),
				"observer": row.get("observer"),
				"source_task": row.get("source_task"),
				"flags": [f for f in str(row.get("stage_flags") or "").splitlines() if f],
			}
		)
	current = latest(block, year)
	age = (_today() - current["_day"]).days if current and current.get("_day") else None
	return {
		"block": block,
		"year": year,
		"current": {"bbch": current.get("growth_stage_code"), "stage": bbch.describe(current.get("growth_stage_code")),
		            "date": current["_day"].isoformat() if current.get("_day") else None, "age_days": age} if current else None,
		"stale": current is None or (age is not None and age > STALE_DAYS),
		"entries": entries,
		"flagged": sum(1 for e in entries if e["flags"]),
	}


def prompt(task: dict, moment: str) -> dict | None:
	"""The stage question for a task's block, or None (decision 27). Never raises."""
	try:
		if str(task.get("location_doctype") or "") != "Field" or not task.get("location"):
			return None
		block = str(task["location"])
		current = latest(block, _today().year)
		if current and current.get("_day") and (_today() - current["_day"]).days <= STALE_DAYS:
			return None
		last = (
			f"Last recorded: BBCH {current.get('growth_stage_code')} on {current['_day']}."
			if current else "No stage recorded this year."
		)
		return {
			"block": block,
			"moment": moment,
			"last_bbch": (current or {}).get("growth_stage_code"),
			"question": {"en": f"What stage is {block} at? {last}",
			             "es": f"¿En qué etapa está {block}? " + (
				             f"Última: BBCH {current.get('growth_stage_code')} el {current['_day']}." if current
				             else "No hay etapa registrada este año.")},
			"tool": "record_growth_stage",
		}
	except Exception:
		frappe.log_error(title="stage prompt failed", message=frappe.get_traceback())
		return None

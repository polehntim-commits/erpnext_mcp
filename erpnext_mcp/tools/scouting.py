# SPDX-License-Identifier: MIT
"""Turning a completed scouting task into the Crop Observation it produced.

A farm already walks its blocks. Somebody is sent to a block, they count what
they were asked to count, they read a Brix off a refractometer, they photograph
what they found and they close the task. Until v0.115.0 all of that landed on a
Farm Task Assignment — the evidence half of the record — and NOTHING landed in
the register that the pest-pressure engine, the harvest-readiness overlay and
next season's threshold argument all read from. The round was worked, evidenced
and paid for, and the map still had nothing to colour.

This module is the join. It is not a new way to record an observation; it is the
recognition that a scouting task's completion ALREADY IS one.

────────────────────────────────────────────────────────────────────────────
WHY IT IS A SWEEP AND NOT A DOCUMENT HOOK
────────────────────────────────────────────────────────────────────────────

`hooks.py` promises this app installs no `doc_events` and
`tests_standalone/test_hooks.py` fails the build over one. `tools/itgc.py` hit
this first and `tools/lots.py` settled it at length for FSMA lot codes: the
producer runs from this app's own tool layer, where an operator can SEE it,
switch it off, and re-run it over last week.

That is not a workaround. A hook that wrote a Crop Observation on every
assignment save would fire on a foreman correcting a typo in a findings note two
weeks later, and it would fire inside somebody else's transaction, where a
refusal from the observation's own controller takes down a completion that was
otherwise fine. The worker is stood in the block when that happens.

A sweep has the opposite failure mode, and it is the affordable one: an
observation that has not been indexed yet is a row that is late, not a row that
is wrong, and `index_scouting_observations` run over the same window twice
writes nothing the second time.

────────────────────────────────────────────────────────────────────────────
THE IDEMPOTENCY KEY IS `Crop Observation.source_task`, NOT THE TASK'S FLAG
────────────────────────────────────────────────────────────────────────────

`Farm Task.produced_record` is STAMPED by this sweep and is not what it trusts.
The authority is the observation register itself: an observation naming this
task exists, or it does not. Trusting the flag would mean a task whose flag was
cleared by hand — or by a half-completed write that stamped the flag before the
insert landed — silently produced a second observation of the same round, which
doubles a block's pest pressure and is invisible from both ends.

So the sweep asks the register, and where it finds an observation on a task
whose flag is blank it REPAIRS the flag rather than writing a second row. That
is the case a `doc_events` hook cannot even see.

────────────────────────────────────────────────────────────────────────────
WHAT IT READS, AND WHY FROM TWO PLACES
────────────────────────────────────────────────────────────────────────────

`Farm Task.creates_record_data` carries WHAT THE SCOUT MEASURED. It is the
template's defaults with the completion's own `record_data` merged over the top
— `complete_farm_task` stamps the merged answer at the moment of completion, so
a template edited next month cannot change what a round already walked said.

The assignment carries WHAT THE COMPLETION KNEW WITHOUT BEING ASKED: the
location fix, the photographs, the findings in the worker's own words, when it
was actually finished and who finished it. Those are never copied onto the task,
because the task is the plan and the assignment is the evidence.

Neither is authoritative over the other, because they answer different
questions. Reading only the task would file an observation with no photograph
and no coordinate; reading only the assignment would file one with no Brix.

────────────────────────────────────────────────────────────────────────────
THE PIPELINE RUNS ON A PEST SCOUT AND ONLY ON A PEST SCOUT
────────────────────────────────────────────────────────────────────────────

See the Crop Observation DocType. A Harvest Readiness round is a Brix and a
growth stage with no organism in it; running a threshold evaluation on it would
either find no threshold (noise) or match one for a pest nobody was looking for
(worse than noise). The evaluation, the pressure upsert and the recommendation
are `cropprotect`'s, called through the three helpers both doors share, so an
observation is evaluated identically whichever way it arrived.

────────────────────────────────────────────────────────────────────────────
IT REPORTS EVERYTHING IT COULD NOT DO, AND REFUSES ALMOST NOTHING
────────────────────────────────────────────────────────────────────────────

The same posture `index_lot_events` takes. A task with no location cannot become
an observation, because an observation IS a block — that is counted and named. A
payload the observation's own controller refuses is counted, named, and does not
stop the row after it. The completion always stands: it was worked, the evidence
is filed, and a sweep that rolled a whole window back over one bad Brix reading
would be a sweep an operator turns off.
"""

from __future__ import annotations

import json

import frappe

from .. import compat
from ..args import as_date, as_str, resolve_company
from ..erpnext_mcp.doctype.crop_observation.crop_observation import (
	OBSERVATION_TYPES,
	PEST_SCOUT,
)
from ..erpnext_mcp.doctype.farm_task.farm_task import AWAITING_REVIEW, COMPLETED
from ..errors import ToolError
from ..result import ToolResult
from . import cropprotect
from . import employee as employee_tool

OBSERVATION = "Crop Observation"
FARM_TASK = "Farm Task"
FARM_TASK_ASSIGNMENT = "Farm Task Assignment"
EMPLOYEE = "Employee"

#: The records a task completion promises but does NOT write, and the sweep that
#: writes each one. `tools/dispatch._produce_record` reads this to tell a
#: deferred producer apart from a doctype it simply has no builder for — the
#: first is "it will be written", the second is "it cannot be". A caller told the
#: wrong one of those goes looking in the wrong place.
DEFERRED_RECORDS = {OBSERVATION: "index_scouting_observations"}

#: The task states whose completion counts. `Awaiting-Review` is here on purpose:
#: it means the completion produced a finding somebody has to look at, which is
#: the round MOST worth having in the register — withholding an observation until
#: a review clears would keep the worst blocks off the map.
INDEXABLE_STATES = (COMPLETED, AWAITING_REVIEW)

#: Most completions one sweep will read. A sweep is run over a week or a season
#: and is meant to finish inside one request; a cap that bites is stated in the
#: answer, never silent. Same figure and same argument as `lots.SWEEP_CAP`.
SWEEP_CAP = 1000

#: What the sweep reads off each completed assignment.
_ASSIGNMENT_FIELDS = (
	"name",
	"task",
	"assigned_to",
	"assigned_to_name",
	"state",
	"company",
	"completed_at",
	"farm_location_gps",
	"findings_text",
	"completion_narrative",
)

#: What it reads off the task the assignment closed.
_TASK_FIELDS = (
	"name",
	"task_name",
	"company",
	"task_type",
	"state",
	"location_doctype",
	"location",
	"creates_record",
	"creates_record_data",
	"produced_record",
	"template",
)

#: The measurement columns a completion may put on the observation, and the only
#: ones it may. A CLOSED LIST RATHER THAN A PASS-THROUGH: `creates_record_data`
#: is operator-editable on the template and client-supplied at completion, so an
#: open merge would let a handset write `threshold_exceeded` — a read-only column
#: the engine computes — and produce a record that claims an evaluation nobody
#: made. A key outside this list is REPORTED, not dropped silently, because a
#: misspelt `brix_readng` otherwise records a round with no Brix in it and looks
#: exactly like a round where nobody took one.
PAYLOAD_FIELDS = (
	"observation_type",
	"threat_category",
	"threat",
	"crop",
	"crop_stage",
	"growth_stage_code",
	"scouting_method",
	"sample_unit",
	"count_observed",
	"sample_size",
	"percent_affected",
	"severity",
	"beneficials_observed",
	"beneficial_name",
	"brix_reading",
	"brix_method",
	"planting_season",
	"notes",
)

#: Evidence types that count as the photograph an observation carries. `Video`
#: is here for the same reason `_unmet_evidence` accepts it against a `photos`
#: contract: a five-second pan of a block edge is better evidence of a mite
#: flare than one still, and a contract that took the still and rejected the pan
#: would be teaching the crew to film less.
PHOTO_TYPES = ("Photo", "Video")


def _require() -> None:
	compat.require_doctype(
		OBSERVATION,
		"the Crop Observation DocType, which ships with erpnext_mcp — run `bench migrate`",
	)
	compat.require_doctype(
		FARM_TASK_ASSIGNMENT,
		"the Farm Task Assignment DocType, which ships with erpnext_mcp — run `bench migrate`",
	)


def index_scouting_observations(args: dict) -> ToolResult:
	"""File the Crop Observations that completed scouting tasks already produced.

	Over one window of completion dates: every Farm Task Assignment closed in it
	whose task produces a Crop Observation becomes one — the measurements off the
	task's `creates_record_data`, the location fix, photograph and findings off
	the assignment, and the threshold engine run on it where it was a pest scout.

	IDEMPOTENT ON `Crop Observation.source_task`. A second sweep over the same
	window writes nothing and says so; a task whose observation exists but whose
	`produced_record` is blank has the flag repaired rather than a second row
	written. See the module docstring for why the register and not the flag is
	the authority.

	IT NEVER ROLLS BACK A WINDOW OVER ONE BAD ROW. A completion whose payload the
	observation's controller refuses is counted and named in `refused`, and the
	sweep carries on — the round was still walked and the evidence is still on
	the assignment, which is a different and better problem than a week of
	scouting that would not index.
	"""
	_require()
	actor = employee_tool.require_shift_role()

	company = resolve_company(as_str(args, "company"), required=False)
	if company:
		employee_tool.require_company_scope(actor, company)

	date_from = as_date(args, "date_from")
	date_to = as_date(args, "date_to")
	if not date_from or not date_to:
		raise ToolError(
			"date_from and date_to are both required. A sweep with no window is a sweep over "
			"every completion on the site, which on a bench with three seasons of dispatch is a "
			"request that does not finish — and an operation indexing its history wants it done a "
			"week at a time so it can read what happened. Nothing was written."
		)
	if date_from > date_to:
		raise ToolError(f"date_from {date_from} is after date_to {date_to}. Nothing was written.")

	written: list = []
	already: list = []
	repaired: list = []
	refused: list = []
	skipped = {
		"completions_without_a_block": 0,
		"completions_of_other_work": 0,
	}
	notes: list = []

	rows = _completions(date_from, date_to, company or "")
	for row in rows:
		task = _task_of(row)
		if not task or str(task.get("creates_record") or "").strip() != OBSERVATION:
			skipped["completions_of_other_work"] += 1
			continue
		if str(task.get("state") or "") not in INDEXABLE_STATES:
			# The assignment closed and the task did not — a rejection reopened
			# it, or a merge moved it. There is no finished round to file.
			skipped["completions_of_other_work"] += 1
			continue

		existing = _observation_of(str(task["name"]))
		if existing:
			already.append(existing)
			if not str(task.get("produced_record") or "").strip():
				_stamp_produced(str(task["name"]), existing)
				repaired.append({"task": task["name"], "observation": existing})
			continue

		if not (task.get("location") and task.get("location_doctype")):
			skipped["completions_without_a_block"] += 1
			continue

		try:
			result = _file_observation(task, row)
		except Exception as exc:  # a bad payload must not cost the window
			# NOTHING IS ROLLED BACK HERE, AND NOTHING NEEDS TO BE. The refusals
			# this catches come out of the observation's own `validate`, which
			# Frappe runs before the insert touches the database — so the failed
			# row wrote nothing. A `frappe.db.rollback()` here would be the one
			# thing this sweep promises not to do: it takes the whole
			# transaction, which is every observation already written in this
			# window, and discards them over one bad Brix reading.
			refused.append(
				{
					"task": task["name"],
					"assignment": row["name"],
					"reason": _reason(exc),
				}
			)
			continue
		written.append(result)

	if skipped["completions_without_a_block"]:
		notes.append(
			f"{skipped['completions_without_a_block']} completion(s) in this window produce a "
			"Crop Observation and name no location. An observation IS a block — the register is "
			"keyed on one — so those rounds are traceable to a task and no further. Set the "
			"task's location and re-run this sweep; nothing was lost."
		)
	if refused:
		notes.append(
			f"{len(refused)} completion(s) could not be filed as observations and are listed in "
			"`refused` with the reason each gave. The completions themselves stand and their "
			"evidence is on the assignment — correct the measurement with "
			"update_farm_task_template or the task's own record data and re-run. A sweep that "
			"rolled the window back over one bad reading would be a sweep nobody leaves on."
		)
	if len(rows) >= SWEEP_CAP:
		notes.append(
			f"this sweep read its {SWEEP_CAP} completion ceiling. There may be more in this "
			"window — narrow the dates and run it again."
		)
	notes.append(
		"THIS IS A SWEEP AND NOT A DOCUMENT HOOK, deliberately: erpnext_mcp installs no "
		"doc_events and its test suite fails the build over one. Re-running it over a window "
		"already indexed writes nothing, which is what makes it safe to schedule and safe to run "
		"twice by accident."
	)

	data = {
		"window": {"date_from": date_from, "date_to": date_to},
		"company": company,
		"observations_written": written,
		"observations_already_present": already,
		"flags_repaired": repaired,
		"refused": refused,
		"skipped": skipped,
		"counts": {
			"completions_read": len(rows),
			"observations_written": len(written),
			"observations_already_present": len(already),
			"flags_repaired": len(repaired),
			"refused": len(refused),
		},
		"actor": actor,
		"notes": notes,
	}
	return ToolResult(
		data=data,
		summary=(
			f"indexed {date_from}..{date_to}: {len(written)} observation(s) written, "
			f"{len(already)} already present" + (f", {len(refused)} refused" if refused else "")
		),
		docstatus_delta="none → 0 (created)" if written else "",
	)


# ── reading the two halves ──────────────────────────────────────────────────


def _completions(date_from: str, date_to: str, company: str) -> list:
	"""Every assignment closed inside the window, newest last.

	THE WINDOW IS ON `completed_at` AND NOT ON THE OBSERVATION DATE, because the
	observation does not exist yet — this is the sweep that would create it. A
	round walked Tuesday and closed Thursday is indexed by a sweep over Thursday
	and lands on the register as a Thursday observation, which is the honest
	answer: the app knows when the task was closed and does not know when
	somebody was standing in the block.
	"""
	filters = {
		"state": COMPLETED,
		"completed_at": ("between", [f"{date_from} 00:00:00", f"{date_to} 23:59:59"]),
	}
	if company and compat.has_field(FARM_TASK_ASSIGNMENT, "company"):
		filters["company"] = company
	return [
		dict(row)
		for row in frappe.db.get_all(
			FARM_TASK_ASSIGNMENT,
			filters=filters,
			fields=compat.existing_fields(FARM_TASK_ASSIGNMENT, _ASSIGNMENT_FIELDS),
			order_by="completed_at asc",
			limit=SWEEP_CAP,
		)
		or []
	]


def _task_of(assignment: dict) -> dict:
	"""The task an assignment closed, or {} where it has since gone."""
	name = str(assignment.get("task") or "")
	if not name:
		return {}
	row = frappe.db.get_value(FARM_TASK, name, compat.existing_fields(FARM_TASK, _TASK_FIELDS), as_dict=True)
	return dict(row) if row else {}


def _observation_of(task: str) -> str:
	"""The observation already filed against this task, by docname. '' where none."""
	if not compat.has_field(OBSERVATION, "source_task"):
		return ""
	return str(frappe.db.get_value(OBSERVATION, {"source_task": task}, "name") or "")


def _stamp_produced(task: str, observation: str) -> None:
	"""Point the task at its observation. Never raises; the register is the truth.

	`update_modified=False` for the reason `backfill_planting_rootstock` gives:
	`modified` is how somebody tells a task that was touched from one nobody has
	been near, and a sweep that stamped it would erase that across the board to
	record something nobody did.
	"""
	try:
		frappe.db.set_value(FARM_TASK, task, "produced_record", observation, update_modified=False)
	except Exception:  # pragma: no cover - a task that vanished mid-sweep
		return


def _photo_of(assignment: str) -> str:
	"""The first photograph filed against a completion, as a URL. '' where none.

	THE FIRST AND NOT ALL OF THEM. `Crop Observation.photo` is one Attach Image,
	and the completion keeps the whole set on its own `evidence_files` — which is
	where an auditor looks and where the chain of custody is. This is the
	thumbnail the register and the map show, and `source_task` is the pointer to
	the rest.
	"""
	child = "Farm Task Evidence"
	if not compat.doctype_exists(child):
		return ""
	rows = (
		frappe.db.get_all(
			child,
			filters={"parent": assignment, "parenttype": FARM_TASK_ASSIGNMENT},
			fields=["file_url", "file", "evidence_type", "idx"],
			order_by="idx asc",
			limit=50,
		)
		or []
	)
	for row in rows:
		if str(row.get("evidence_type") or "") not in PHOTO_TYPES:
			continue
		url = str(row.get("file_url") or "").strip()
		if url:
			return url
	return ""


def _observer_of(worker: str) -> str:
	"""The User an Employee id logs in as, where the site knows. '' where not.

	`Farm Task.assigned_to` is an EMPLOYEE ID and `Crop Observation.observer` is
	a Link to User, and the two are different registers — Frappe HR is not a
	dependency of this app. Writing the employee id into a User link would break
	the link on every real bench, so where the mapping is not on file the column
	is left blank and the scout's name goes into the notes instead. A blank
	observer beside a named scout in the notes is a gap somebody can close; a
	broken link is a record that will not open.
	"""
	if not (worker and compat.doctype_exists(EMPLOYEE) and compat.has_field(EMPLOYEE, "user_id")):
		return ""
	user = str(frappe.db.get_value(EMPLOYEE, worker, "user_id") or "").strip()
	if not user:
		return ""
	return user if frappe.db.exists("User", user) else ""


# ── writing the observation ─────────────────────────────────────────────────


def _payload_of(task: dict) -> tuple:
	"""(measurements, unknown_keys) off the task's stamped record data.

	See `PAYLOAD_FIELDS` for why the list is closed. Unknown keys come back so
	the caller can put them in the observation's notes rather than dropping them:
	somebody typed them meaning something, and a silently discarded `brix_readng`
	is indistinguishable from a round where nobody took a reading.
	"""
	raw = task.get("creates_record_data")
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
	except Exception:
		value = {}
	if not isinstance(value, dict):
		value = {}
	measurements = {key: value[key] for key in PAYLOAD_FIELDS if key in value}
	unknown = sorted(set(value) - set(PAYLOAD_FIELDS))
	return measurements, unknown


def _file_observation(task: dict, assignment: dict) -> dict:
	"""One completed scouting round, as a Crop Observation. Raises on a bad payload."""
	measurements, unknown = _payload_of(task)

	observation_type = str(measurements.get("observation_type") or "").strip() or PEST_SCOUT
	if observation_type not in OBSERVATION_TYPES:
		raise ToolError(
			f"this task's record data names observation_type {observation_type!r}, which is not "
			f"one of: {', '.join(OBSERVATION_TYPES)}. Nothing was written for this completion."
		)

	completed_at = str(assignment.get("completed_at") or "")
	observed_on = (completed_at.split(" ")[0] if completed_at else "") or frappe.utils.nowdate()
	company = str(task.get("company") or assignment.get("company") or "")

	doc = frappe.new_doc(OBSERVATION)
	doc.company = company or None
	doc.block_doctype = str(task.get("location_doctype") or "")
	doc.block = str(task.get("location") or "")
	doc.observation_type = observation_type
	doc.observed_on = observed_on
	doc.observed_at = completed_at or None
	doc.observer = _observer_of(str(assignment.get("assigned_to") or "")) or None
	doc.observed_gps = str(assignment.get("farm_location_gps") or "") or None
	doc.source_task = task["name"]
	doc.photo = _photo_of(str(assignment["name"])) or None

	for field in PAYLOAD_FIELDS:
		if field in ("observation_type", "notes") or field not in measurements:
			continue
		value = measurements[field]
		if value in (None, ""):
			continue
		doc.set(field, value)

	doc.notes = _notes(task, assignment, measurements, unknown)

	# The evaluation, on a pest scout and only there. See the module docstring.
	threshold = None
	evaluation = None
	if observation_type == PEST_SCOUT and str(doc.threat or "").strip():
		threshold, evaluation = cropprotect.evaluate_against_threshold(
			company,
			str(doc.crop or ""),
			str(doc.threat or ""),
			str(doc.crop_stage or ""),
			_number(doc.count_observed),
			int(doc.sample_size or 0),
			_number(doc.beneficials_observed),
			str(doc.sample_unit or ""),
		)
		cropprotect.stamp_evaluation(doc, threshold, evaluation)

	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	downstream = {"pest_pressure": None, "ipm_recommendation": None}
	if evaluation is not None:
		downstream = cropprotect.run_downstream(doc, threshold, evaluation, int(str(observed_on)[:4]))

	_stamp_produced(str(task["name"]), doc.name)

	return {
		"observation": doc.name,
		"task": task["name"],
		"assignment": assignment["name"],
		"observation_type": observation_type,
		"block": doc.block,
		"block_doctype": doc.block_doctype,
		"observed_on": str(observed_on),
		"brix_reading": doc.brix_reading if doc.brix_reading not in (None, "") else None,
		"brix_method": doc.brix_method or None,
		"growth_stage_code": doc.growth_stage_code or None,
		"observed_gps": doc.observed_gps or None,
		"photo": doc.photo or None,
		"pest_pressure": (downstream["pest_pressure"] or {}).get("name")
		if downstream["pest_pressure"]
		else None,
		"ipm_recommendation": (downstream["ipm_recommendation"] or {}).get("name")
		if downstream["ipm_recommendation"]
		else None,
		"evaluation_note": doc.evaluation_note or None,
		"unrecognised_record_data_keys": unknown or None,
	}


def _notes(task: dict, assignment: dict, measurements: dict, unknown: list) -> str:
	"""The observation's notes: the scout's own words first, provenance after.

	THE WORKER'S FINDINGS GO IN UNEDITED AND FIRST. They are the only part of
	this record somebody wrote rather than the app assembled, and burying them
	under a provenance line is how a note stops being read. Everything after is
	the app saying where the row came from, which is what makes an observation
	that nobody typed into the register auditable at all.
	"""
	parts = []
	findings = str(assignment.get("findings_text") or "").strip()
	narrative = str(assignment.get("completion_narrative") or "").strip()
	stated = str(measurements.get("notes") or "").strip()
	for part in (findings, narrative, stated):
		if part and part not in parts:
			parts.append(part)

	scout = str(assignment.get("assigned_to_name") or assignment.get("assigned_to") or "").strip()
	provenance = (
		f"Filed by index_scouting_observations from {task['name']}"
		+ (f" ({task.get('task_name')})" if task.get("task_name") else "")
		+ f", completed on assignment {assignment['name']}"
		+ (f" by {scout}" if scout else "")
		+ ". The photographs, the signature and the evidence contract this round was closed "
		"against are on the assignment."
	)
	parts.append(provenance)

	if unknown:
		parts.append(
			"The task's record data also carried "
			+ ", ".join(repr(key) for key in unknown)
			+ ", which is not a Crop Observation measurement this sweep writes. Kept here rather "
			"than dropped: a misspelt column otherwise records a round with the reading missing "
			"and looks exactly like a round where nobody took one."
		)
	return "\n\n".join(parts)


def _number(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def _reason(exc: Exception) -> str:
	"""One line naming why a completion could not be filed, for the report."""
	message = " ".join(str(getattr(exc, "message", "") or exc).split()).strip()
	return f"{type(exc).__name__}: {message}" if message else type(exc).__name__

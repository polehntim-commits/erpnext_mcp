# SPDX-License-Identifier: MIT
"""The methods the Farm Ops app calls, as whitelisted Frappe endpoints.

    POST /api/method/erpnext_mcp.api.mobile.<method>
    Authorization: token <api_key>:<api_secret>
    X-FarmOps-Token: <api_key>:<api_secret>

THE SECOND HEADER IS NOT BELT-AND-BRACES FOR ITS OWN SAKE. v0.17.2: the Tailscale
`serve`/`funnel` proxy removes `Authorization`, so every call arrived as Guest and
Frappe rendered `/me` at a phone that had presented a perfectly good credential.
`api/fallback_auth.py` reads the same pair out of `X-FarmOps-Token`, or out of
`_auth` in the POST body when even that does not survive, and establishes the
identical session. Every gate below runs unchanged on whichever door was used.

One function per method, named exactly as
`FarmOpsKit/Sources/FarmOpsKit/Networking/MobileAPI.swift` names it. THERE IS NO
DISPATCHER HERE ON PURPOSE: a method exists as a function or its path 404s, so
the whole reachable surface is the `@frappe.whitelist()` lines below and an
auditor establishes that by reading them. A generic `call(tool_name, args)`
would have been fewer lines and would have published all two hundred MCP tools —
including `create_journal_entry`, `convey_parcel` and `import_chart_of_accounts`
— to anything holding a field worker's phone.

EACH WRAPPER DOES FOUR THINGS AND NO MORE:

    validate the arguments  →  delegate  →  shape for the app  →  return

`@guard.endpoint` has already run the kill switch, the role gate, the enrolment
gate and the rate limit by the time the body starts, and writes the audit row
and strips secrets on the way out whichever way the call went. The body's own
job is the part that is specific to this method: check the docnames, refuse the
companies this caller cannot reach, and never pass an argument through blind.

WHAT IS DELIBERATELY *NOT* PASSED THROUGH is as much the design as what is.
`reject_farm_task` takes `cancel=true`, which cancels a task outright instead of
returning it to the pool; a worker handing work back must not be able to delete
it, so the wrapper never forwards it. `complete_farm_task` takes `record_data`,
which writes arbitrary fields into the compliance record it produces; the phone
has no business composing that. `list_dispatched_tasks` takes `worker_id`, and
the wrapper declares `employee` instead and refuses any name that is not on the
caller's own crew — an account that can name somebody else in a request body is
not scoped to anything, and a foreman's crew is what scopes this one.

THE RULES STAY IN `tools/dispatch.py`. The concurrent-claim limit, the refusal
to self-pick Dispatched work, the evidence contract and the refusal of a
completion filed by somebody who was not there are all still enforced by Sprint
8's code, because it IS Sprint 8's code. A wrapper with its own copy would be a
second set of compliance rules to keep in step, and they would drift.

────────────────────────────────────────────────────────────────────────────
THE ONBOARDING, CREW-CLOCK AND BUCKET METHODS CARRY A SECOND ROLE GATE
────────────────────────────────────────────────────────────────────────────

v0.45.0 published nine more, v0.46.0 three more again and v0.46.2 a thirteenth,
and they are not like the first fifteen. Every tool the original wrappers
delegate to is field work with no role check of its own; these thirteen reach
`tools/employee.py`, `tools/i9.py`, `tools/w4.py`, `tools/shifts.py` and
`tools/bucket_log.py`, and each of THOSE calls `employee.require_hr_role()` or
`kpi.require_kpi_role()` before it writes a row.

`get_employee` is the one with an EXCEPTION in its gate rather than a copy of it,
and the exception is a single record: a worker reading their OWN Employee row —
their hire date, their I-9 status, the badge in their pocket — is not reading the
personnel register, and the wizard's returning-worker path is the reason to say
so. Everybody else's record still wants the HR role. That is written out in the
wrapper's own docstring, because a gate with a hole in it is the gate somebody
has to be able to find.

THAT GATE IS LEFT EXACTLY WHERE IT IS, and the consequence is stated here
rather than discovered in an orchard: of the roles `guard.FARM_OPS_ROLES`
admits, only **Farm Manager** is also in `employee.HR_ROLES` and
`kpi.KPI_ROLES`. A Field Worker or a Foreman holding a perfectly good grant
gets through all seven of `guard`'s gates and is then refused by the tool with
its own sentence. That is the correct refusal — an I-9 is a personnel record
and it is not a picker's to write — but it means an operator enrolling somebody
to run onboarding must enrol them as a Farm Manager, or grant the account one of
the HR roles in the Desk.

THE CREW CLOCK IS THE EXCEPTION AND IT IS THE TOOL'S OWN LIST THAT CHANGED,
not a copy of the gate up here. `tools/shifts.py` gates on
`employee.SHIFT_ROLES` — the HR roles plus Foreman and Crew Leader — because a
shift is the one register whose obligations OAR 437-004-1131 puts on the NAMED
supervisor, and because this app's own role table already grants Foreman full
permission on `Farm Shift`. The observable failure was a handset showing a Crew
Clock button to exactly the roles the server then refused. Hiring did not move
with it: those two roles still cannot create or edit an Employee, an I-9 or a
W-4.

Copying either gate up here, or widening one at this layer, would be the same
mistake the paragraph above refuses for the dispatch rules: two sets of
personnel rules to keep in step.
"""

from __future__ import annotations

import base64
import json

import frappe

from .. import bucket_bridge, compat, datetimes, locations, overlays, pay_stub_pdf, timezones
from .. import roles as role_lib
from .. import shifts as shift_records
from .. import training as training_register
from ..erpnext_mcp.doctype.crop_observation import crop_observation as observation_rules
from ..erpnext_mcp.doctype.farm_task_assignment import farm_task_assignment as assignment_states
from ..errors import ToolError
from ..tools import accidents as accident_tools
from ..tools import ach as ach_tools
from ..tools import agronomy as agronomy_tools
from ..tools import app_feedback as feedback_tools
from ..tools import (
	asset_tags,
	badges,
	bucket_log,
	crew_view,
	dispatch,
	docvalidation,
	fieldwork,
	i9,
	shifts,
	signatures,
	signed_documents,
	signers,
	w4,
)
from ..tools import binseals as bin_seal_tools
from ..tools import calendar as compliance_calendar
from ..tools import compintel as compintel_tools
from ..tools import dimensions as dimension_tools
from ..tools import discipline as discipline_tools
from ..tools import employee as personnel
from ..tools import evidence as evidence_tools
from ..tools import expenses as expense_tools
from ..tools import farm as farm_tools
from ..tools import files as file_tools
from ..tools import haccp as haccp_tools
from ..tools import housing as housing_tools
from ..tools import iot as iot_tools
from ..tools import locations as location_tools
from ..tools import lots as lot_tools
from ..tools import map_overlays as map_overlay_tools
from ..tools import masters as master_tools
from ..tools import ml_model as ml_model_tools
from ..tools import mobile as mobile_tools
from ..tools import mrl as mrl_tools
from ..tools import narrative as narrative_tools
from ..tools import org as org_tools
from ..tools import payroll as payroll_tools
from ..tools import payroll_deductions as payroll_deduction_tools
from ..tools import push as push_tools
from ..tools import realestate as realestate_tools
from ..tools import receipts as receipt_tools
from ..tools import scouting as scouting_tools
from ..tools import sessions as session_tools
from ..tools import shadow_log as shadow_log_tools
from ..tools import shipments as shipment_tools
from ..tools import spray as spray_tools
from ..tools import stock_inventory as stock_tools
from ..tools import strategy as strategy_tools
from ..tools import tasktemplates as template_tools
from ..tools import tax_remittance as remittance_tools
from ..tools import trace as trace_tools
from ..tools import training as training_tools
from ..tools import training_sessions as training_session_tools
from ..tools import universal_scan as universal_scan_tool
from ..tools import valves as valve_tools
from ..tools import wallet as wallet_tools
from ..tools import wizards as wizard_tools
from . import fallback_auth, guard, rectify, shape

ALERT = "Compliance Alert"
FARM_TASK = "Farm Task"
FARM_TASK_ASSIGNMENT = "Farm Task Assignment"
EMPLOYEE = "Employee"
FARM_SHIFT = "Farm Shift"
ML_MODEL = "ML Model"
HOUSING_UNIT = "Housing Unit"
HOUSING_ASSIGNMENT = "Housing Assignment"
CERTIFICATION = "Certification"
TRAINING_RECORD = "Employee Training Record"
TRAINING_SESSION = "Training Session"
REGULATORY_FILING = "Regulatory Filing"
COMPLIANCE_POLICY = "Compliance Policy"
EXPENSE_RECEIPT = "Expense Receipt"
PAYROLL_DEDUCTION = "Farm Payroll Deduction"
DOCUMENT_VALIDATION = "Document Validation"
DISCIPLINE_RECORD = "Farm Incident Record"
ACCIDENT_REPORT = "Accident Report"
FARM_TASK_TEMPLATE = template_tools.TEMPLATE
SHADOW_LOG_ENTRY = shadow_log_tools.DOCTYPE
CROP_OBSERVATION = scouting_tools.OBSERVATION

#: The Farm Task Assignment state a completion lands in. Imported from the
#: doctype's own controller rather than spelled here, so a vocabulary change
#: cannot leave this module quietly matching a state that no longer exists.
ASSIGNMENT_COMPLETED = assignment_states.COMPLETED

#: Most crew members `list_dispatched_tasks` will read a board for in one call.
#: `shifts.CREW_CAP` is what `start_shift` will roster and is read rather than
#: restated, so a phone and the crew clock cannot come to disagree about how big
#: a crew is. It matters here because the board is read ONE WORKER AT A TIME —
#: see the wrapper on why that is the right cost.
CREW_BOARD_CAP = shifts.CREW_CAP

#: Most templates the picker hands back. `tasktemplates` caps the register at 200
#: and this is the same number: a template list is a screen somebody scrolls to
#: find the job they are about to raise, and an operation with more standing jobs
#: than this has a register question rather than a paging one.
TEMPLATE_LIST_LIMIT = 200

#: The four HR masters the wizard's Assignment step offers as dropdowns, mapped
#: to the field on each that carries a human label. `Branch` has no second
#: column at all on a stock Frappe HR — the docname IS the branch name — which
#: is why the value here may be empty and `label` falls back to the docname.
REFERENCE_MASTERS = (
	("branches", "Branch", "branch"),
	("departments", "Department", "department_name"),
	("designations", "Designation", "designation_name"),
	("employment_types", "Employment Type", "employee_type_name"),
)

#: Most rows any one reference list hands back. A dropdown longer than this is a
#: dropdown nobody scrolls, and every one of these masters is a hand-maintained
#: table on a real site — a farm with two hundred designations has a data problem
#: rather than a paging problem.
REFERENCE_LIMIT = 200

#: Most Housing Units `list_available_housing` reads. `tools/housing.REGISTER_CAP`
#: is the register's own ceiling and is read rather than restated so a phone and
#: a Desk report cannot come to disagree about how big a camp is.
HOUSING_LIST_LIMIT = housing_tools.REGISTER_CAP

#: Most bucket captures one sync call carries. `tools/bucket_log.BATCH_CAP` is
#: the tool's own limit and is read rather than restated, so a phone and a Desk
#: import cannot come to disagree about how big a batch is.
BUCKET_BATCH_CAP = bucket_log.BATCH_CAP

#: Most rows `search_employees` will hand back. The number iOS asks Frappe's REST
#: list endpoint for in `OnboardingAPI.searchEmployees`, kept exactly: the search
#: field on the wizard's Identity step shows a scrolling list a person picks one
#: name out of, and a longer answer is a bigger payload nobody reads to the end.
#: A search that hits the cap is a search that needs another letter typed into it.
EMPLOYEE_SEARCH_LIMIT = 20

#: Most scale tickets the capture screen's back-button list hands back. It is a
#: "what did I just file" list rather than a register view — twenty covers a
#: morning at a bin trailer, and anything longer is a question for the Desk.
MOBILE_TICKET_LIMIT = 20

#: What `search_employees` reports about each match, in the order
#: `ExistingEmployee` (`Models/OnboardingModels.swift:273`) declares them.
#: `company` is not in that struct and is emitted anyway — `guard.scoped` checks
#: the result against the caller's entities on the way out, and it needs the
#: column to check.
EMPLOYEE_SEARCH_FIELDS = (
	"name",
	"employee_name",
	"employee_number",
	"status",
	"date_of_joining",
	"employment_type",
	"company",
	# v0.106.0. THE TWO COLUMNS A PICKER NEEDS AND THIS SEARCH WAS DROPPING.
	# `designation` is the job title — Checker, Tractor Driver, Foreman — and
	# `user_id` is the login the person's ROLES hang off. Neither is displayed
	# raw: `designation` goes out as itself, and `user_id` is spent resolving
	# `capability` and is NOT returned, because a login is a credential
	# identifier and a roster read has no business handing one to a handset.
	"designation",
	"user_id",
)


def _capability(user_id) -> dict:
	"""One employee's role capability, flattened for a roster row. v0.106.0.

	`user_id` IS SPENT HERE AND NEVER RETURNED. It is read so the roles hanging
	off that login can be, and a search result carrying it would hand a handset
	the login of every person on the register — which is the identifier an
	attacker needs before a password is worth guessing. The three keys that come
	back say what the picker has to know and nothing about how to sign in as
	anybody.
	"""
	answer = role_lib.capability_of(user_id)
	return {
		"mobile_roles": answer["mobile_roles"],
		"primary_role": answer["primary_role"],
		"can_dispatch": answer["can_dispatch"],
		# v0.108.0, S11. THE SAME BADGE THE CALLER GETS FOR THEMSELVES in
		# `get_current_user_context`, so a roster row and the account screen draw
		# the same word for the same person. `primary_role` above is NOT that
		# word and cannot be made into it: it is `held[0]` in `ROLE_SPECS` order,
		# which is the LEAST of what somebody holds, so a foreman who is also a
		# field worker reads as "Field Worker" on a picker. See
		# `roles.ROLE_INDICATORS` for why the badge needs its own precedence.
		"role_indicator": role_lib.role_indicator(user_id),
	}


def _employee(user: str) -> str:
	"""The caller's Employee docname, or the refusal that says how to fix it."""
	employee = fieldwork._employee_for(user)
	if not employee:
		frappe.throw(
			f"{user} has no Employee record on this site, and a task assignment names an "
			"Employee rather than a login. Ask an operator to set `user_id` on your Employee "
			"record to this address.",
			frappe.ValidationError,
		)
	return employee


def _company(user: str, company, allowed: list) -> str:
	"""One company argument, validated, falling back to the caller's first entity.

	`guard.require_company` answers "" for "nothing was asked for", which every
	READ in this file reads as "all of mine". A write has to name exactly one, and
	the app sends it on every onboarding call — so the fallback is for the phone
	that does not, and it can only ever pick an entity this account already
	reaches, because `guard.require_scope` refused it otherwise.
	"""
	return guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")


def _employee_argument(employee, allowed: list, label: str = "employee") -> str:
	"""An Employee docname from the body, proved to exist inside the caller's entities.

	NOT the caller's own Employee — that is `_employee`, and the two are different
	on purpose. Onboarding and the crew clock are things somebody does TO another
	person's record, so the docname has to come from the body; what makes that
	safe is that it is checked against `Employee.company` the same way a task
	docname is, and an Employee of an entity this account cannot see reads as not
	found rather than as refused.
	"""
	return guard.require_scoped_doc(EMPLOYEE, employee, label, allowed)


def _assignment(task: str, assignment, allowed: list) -> str:
	"""An optional task_assignment argument, validated and proved to fit the task.

	The app usually sends one and the tools can find it without help. When it IS
	sent it is checked BOTH ways — that it exists within the caller's entities,
	and that it actually belongs to the task named alongside it — because an
	assignment docname from another task is the one argument here that could
	otherwise move work between records.
	"""
	value = str(assignment or "").strip()
	if not value:
		return ""
	value = guard.require_scoped_doc(FARM_TASK_ASSIGNMENT, value, "task_assignment", allowed)
	if str(frappe.db.get_value(FARM_TASK_ASSIGNMENT, value, "task") or "") != task:
		frappe.throw(f"task_assignment {value} does not belong to {task}.", frappe.ValidationError)
	return value


def _last_completed(history) -> dict:
	"""The most recent COMPLETED assignment in a task's history, or `{}`.

	v0.76.0, and it exists for one reason: `live_assignment` excludes the state
	the app most needs the timestamps from. A completed assignment is not live —
	correctly, nothing is waiting on it — but it is the one carrying
	`completed_at` and `actual_duration_minutes`, which is what stops a handset
	counting elapsed time forward from `started_at` forever.

	REJECTED ASSIGNMENTS ARE NOT ELIGIBLE. A worker who handed the job back has a
	`started_at` and no completion, and shaping the task against theirs would put
	a start time on a task nobody finished — the same open-ended timer, with a
	name against it. `get_farm_task` orders history newest-first, so the first
	match is the latest completion.
	"""
	for entry in history or []:
		if isinstance(entry, dict) and entry.get("state") == ASSIGNMENT_COMPLETED:
			return entry
	return {}


def _evidence(raw) -> list:
	"""The app's evidence list, translated to what `complete_farm_task` takes.

	The phone knows `{"file_token", "file_name", "sha256", "kind", "phase"}`
	because that is what `finalize_staged_file` handed it plus the one label the
	camera screen collected; the tool wants `{"file", "evidence_type", "phase"}`. `file_token` IS the File docname — see
	`api/files.finalize_staged_file`, which is the only thing that mints one — so
	this is a rename rather than a lookup, and `normalise_evidence` still refuses
	any docname that is not a real File on this site.

	The sha256 the app carries was already verified against the assembled bytes
	at finalize, and that verification is what the audit row for the upload
	records. Farm Task Evidence has no hash column to put it in, so it is not
	silently dropped into `caption` — see RELEASES/v0.17.1.md, which names the
	column as the follow-up if the hash should live on the record itself.
	"""
	if raw in (None, ""):
		return []
	if isinstance(raw, (str, dict)):
		raw = [raw]
	if not isinstance(raw, list):
		raise ToolError(
			'evidence_files must be a list of objects like {"file_token": "...", "kind": "photo"}.'
		)
	kinds = {"photo": "Photo", "signature": "Signature", "video": "Video", "document": "Document"}
	out = []
	for index, entry in enumerate(raw):
		if isinstance(entry, str):
			out.append({"file": entry, "evidence_type": "Photo"})
			continue
		if not isinstance(entry, dict):
			raise ToolError(f"evidence_files[{index}] must be an object.")
		token = str(entry.get("file_token") or entry.get("file") or "").strip()
		url = str(entry.get("file_url") or "").strip()
		if not (token or url):
			raise ToolError(f"evidence_files[{index}] names neither a file_token nor a file_url.")
		kind = str(entry.get("kind") or entry.get("evidence_type") or "photo").strip().lower()
		row = {"evidence_type": kinds.get(kind, "Photo")}
		if token:
			row["file"] = token
		if url:
			row["file_url"] = url
		if entry.get("file_name"):
			row["caption"] = str(entry["file_name"])[:140]
		# v0.96.0. WHETHER THIS FRAME IS THE BEFORE ONE. `Farm Task Evidence`
		# gained a `phase` column in this release; until it did, the handset
		# wrote the answer into the filename (`FT-…_photo_before_….jpg`) because
		# an unrecognised fifth key risked a strict validator refusing the whole
		# completion — and a completion is the one submission on this surface a
		# worker cannot redo, because the cabin has been cleaned and the crew has
		# gone home.
		#
		# AN UNRECOGNISED PHASE IS DROPPED RATHER THAN RAISED ON, which is the
		# posture this whole normaliser already takes — an unknown `kind` becomes
		# Photo two lines up rather than failing the call. The trade is the same
		# one and it is decided the same way: losing a label is recoverable from
		# the filename, and losing the photographs is not. A caller who wants the
		# strict refusal has it on the MCP tool, where `normalise_evidence`
		# checks the value against the doctype's own list.
		phase = str(entry.get("phase") or "").strip().lower()
		if phase in ("before", "after"):
			row["phase"] = phase
		out.append(row)
	return out


def _location(given, latitude, longitude) -> str:
	"""Where the work was done, as one string, from whichever half the app sent.

	v0.19.1. A TYPED PLACE NAME BEATS A COORDINATE. The handset's fix is the
	usual answer, but a worker who wrote "MC-Cabin-01" did so in a shed where
	the fix was absent or wrong, and overwriting that with whatever the GPS
	eventually settled on outside would replace a fact with a guess.

	A pair that will not parse as numbers is DROPPED rather than raised on. The
	field is optional and additive; failing a completion — with its photographs,
	its signature and its compliance record — over a malformed coordinate would
	trade the whole record for its least important field. The latitude and
	longitude as sent are in the audit row regardless, which is where a
	malformed pair is worth looking at anyway.
	"""
	typed = str(given or "").strip()
	if typed:
		return typed[:140]
	if latitude in (None, "") or longitude in (None, ""):
		return ""
	try:
		return f"{float(latitude):.7f},{float(longitude):.7f}"
	except (TypeError, ValueError):
		return ""


#: The measurement columns a completion filed from a handset may put on the Crop
#: Observation it produces, and the only four. NAMED ARGUMENTS RATHER THAN
#: `record_data`, which stays unreachable from this transport for the reason
#: `complete_task_via_mobile` gives at length: an open dictionary writes
#: arbitrary fields into a compliance record. Four names write four columns.
#:
#: The pest-scout half — `threat`, `threat_category`, `count_observed`,
#: `sample_size` — is deliberately NOT here. It carries the threshold engine
#: behind it, and whether a handset should be able to move a block's pest
#: pressure is a decision worth making on its own rather than one that arrives
#: as a consequence of letting a scout record the Brix.
MEASUREMENT_ARGUMENTS = ("observation_type", "growth_stage_code", "brix_reading", "brix_method")


def _as_text(value) -> str:
	"""One client-supplied value as text. NOT `str(value or "")`, which eats zero.

	v0.119.0. `0 or ""` is `""`, so the usual normalising idiom turns a
	legitimate zero into an absent argument — and every check downstream then
	agrees the caller said nothing. `_measurements` is where that mattered: a
	BBCH stage of 0 passed the `not in (None, "")` filter, so the function did
	not return early, then vanished at `if code:` and threw nothing. The round
	filed with the template's default and a null growth stage — a walk somebody
	did, recorded as a walk nobody did, which is the exact failure v0.117.0 was
	written to end, re-entering through a type coercion instead of a missing
	argument.

	See `erpnext_mcp/tools/farm.py:_stage` for the same trap in its comparison
	form, and prefer an explicit `is None` test anywhere a value may be zero.
	"""
	return "" if value is None else str(value).strip()


def _record_defaults(raw) -> dict:
	"""The task's stamped `creates_record_data`, tolerant of a blob nobody can parse.

	Read-side, so a task holding bad JSON is treated as holding nothing rather
	than costing somebody their completion. The write side of that field is
	`create_farm_task`, which refuses.
	"""
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
	except Exception:
		return {}
	return value if isinstance(value, dict) else {}


def _measurements(task: str, observation_type, growth_stage_code, brix_reading, brix_method) -> dict:
	"""The four scouting readings off the arguments, refused early and by name.

	v0.117.0. `SERVER_CHANGES.md` §26. A scouting round closed from the app used
	to file an observation carrying the seeded template's defaults and NOTHING
	ELSE: `brix_reading` and `growth_stage_code` both null on a round where
	somebody stood in the block and read both. `overlays.harvest_overlay` then
	drew that block grey with `short_of` reporting that nobody took a reading.
	The numbers were in the findings text and legible to a person; they were not
	in the numeric columns the map reads.

	EVERY REFUSAL BELOW HAPPENS HERE RATHER THAN AT THE SWEEP, and that is the
	whole reason this function exists instead of four pass-through lines. The
	Crop Observation is written days later by `index_scouting_observations`, and
	a payload its controller refuses is counted in that sweep's `refused` list —
	correct, and read by nobody, a week after the phone that could have corrected
	it left the block. The person who took the reading is standing there NOW.

	`Pest Scout` IS REFUSED FROM THIS DOOR ON PURPOSE. The register requires a
	threat, a category and a count on that type, none of which this transport
	can send (see `MEASUREMENT_ARGUMENTS`), so accepting the word would stamp a
	round that the sweep is then obliged to refuse — the exact silent failure
	above, arrived at from the other direction. A task whose TEMPLATE says Pest
	Scout is untouched: this only refuses a handset asking for one.

	THE BRIX PAIR IS CHECKED AGAINST THE TASK'S OWN DEFAULTS, not against the
	submission alone. A template that stamps `brix_method: Refractometer` has
	already answered "how was it read", and a phone sending the number would be
	refused for a missing half that is on the record in front of it. The check
	runs only where this submission touched one of the two — a template's own
	standing default is never something a completion has to justify.
	"""
	sent = {
		key: value
		for key, value in (
			("observation_type", observation_type),
			("growth_stage_code", growth_stage_code),
			("brix_reading", brix_reading),
			("brix_method", brix_method),
		)
		if value not in (None, "")
	}
	if not sent:
		return {}

	row = frappe.db.get_value(FARM_TASK, task, ["creates_record", "creates_record_data"], as_dict=True) or {}
	produces = str(row.get("creates_record") or "").strip()
	if produces != CROP_OBSERVATION:
		frappe.throw(
			f"This task produces {produces or 'no compliance record'}, so there is nowhere to put "
			f"{', '.join(sorted(sent))}. The scouting readings belong to a task whose record is a "
			f"{CROP_OBSERVATION} — one raised from a scouting template. Nothing was filed: send the "
			"completion again without them, and the work still closes.",
			frappe.ValidationError,
		)

	out = {}

	kind = _as_text(observation_type)
	if kind:
		match = next(
			(known for known in observation_rules.OBSERVATION_TYPES if known.lower() == kind.lower()), ""
		)
		if not match:
			frappe.throw(
				f"observation_type is {kind!r}, which is not a kind of round this register knows. "
				f"It is one of: {', '.join(observation_rules.OBSERVATION_TYPES)}. Nothing was filed.",
				frappe.ValidationError,
			)
		if match == observation_rules.PEST_SCOUT:
			frappe.throw(
				f"A {observation_rules.PEST_SCOUT} observation is a count of a named organism, and "
				"the threat, its category and the count cannot be sent from a handset — so this "
				"round would be stamped and then refused by the register days later, with nobody "
				"in the block to correct it. File the maturity or condition round as "
				f"{', '.join(k for k in observation_rules.OBSERVATION_TYPES if k != observation_rules.PEST_SCOUT)}, "
				"or raise the pest count from the Desk. Nothing was filed.",
				frappe.ValidationError,
			)
		out["observation_type"] = match

	code = _as_text(growth_stage_code)
	if code:
		out["growth_stage_code"] = code[:140]

	method = _as_text(brix_method)
	if method:
		known = next((m for m in observation_rules.BRIX_METHODS if m and m.lower() == method.lower()), "")
		if not known:
			frappe.throw(
				f"brix_method is {method!r}. It is one of: "
				f"{', '.join(m for m in observation_rules.BRIX_METHODS if m)} — a refractometer "
				"figure and somebody's estimate are not the same measurement and must not average "
				"together. Nothing was filed.",
				frappe.ValidationError,
			)
		out["brix_method"] = known

	if brix_reading not in (None, ""):
		try:
			reading = float(brix_reading)
		except (TypeError, ValueError):
			frappe.throw(
				f"brix_reading is {brix_reading!r}, which is not a number. Send the degrees Brix "
				"as a figure — 18.5 — or leave it out. Nothing was filed.",
				frappe.ValidationError,
			)
		if reading < 0:
			frappe.throw("brix_reading cannot be negative. Nothing was filed.", frappe.ValidationError)
		if reading > observation_rules.BRIX_CEILING:
			frappe.throw(
				f"brix_reading is {reading:g}, which is above {observation_rules.BRIX_CEILING:g} — the "
				"ceiling this app accepts on fruit. Ripe sweet cherries run 16-24 and table grapes to "
				"about 26, so a figure this high is almost always a decimal point in the wrong place. "
				"Nothing was filed.",
				frappe.ValidationError,
			)
		out["brix_reading"] = reading

	if "brix_reading" in out or "brix_method" in out:
		merged = {**_record_defaults(row.get("creates_record_data")), **out}
		has_reading = merged.get("brix_reading") not in (None, "")
		has_method = bool(str(merged.get("brix_method") or "").strip())
		if has_reading and not has_method:
			frappe.throw(
				f"A Brix reading was sent with no brix_method, and the task does not carry one. Say "
				f"whether it came off a refractometer or was estimated: "
				f"{', '.join(m for m in observation_rules.BRIX_METHODS if m)}. The two are not the "
				"same measurement, and the number that gets quoted into a buyer's specification is "
				"the one nobody can tell apart afterwards. Nothing was filed.",
				frappe.ValidationError,
			)
		if has_method and not has_reading:
			frappe.throw(
				"brix_method was sent with no Brix reading, and the task does not carry one. Stating "
				"how a number was taken when there is no number is a record that reads as "
				"instrumented and holds nothing. Nothing was filed.",
				frappe.ValidationError,
			)

	return out


def _bucket_entries(raw, company: str) -> list:
	"""The handset's capture queue, translated to what `sync_bucket_entries` takes.

	THE COMPANY IS STAMPED ON EVERY ENTRY FROM THE CALL, NEVER READ OFF ONE. The
	tool resolves `company` per entry, which is right for a Desk import that may
	legitimately carry two entities in one file. On this transport it would be a
	hole: one batch could write Bucket Log Entry rows against an entity the caller
	cannot see, and a picking record is a payroll record. So the wrapper takes ONE
	company argument, checks it against the caller's scope once, and overwrites
	whatever each entry claimed.

	`employee` IS NOT ACCEPTED ON AN ENTRY, for the same reason `list_my_tasks`
	fills `worker_id` from the session. The badge is what attributes a bucket, the
	Bucket Log Badge Map is what resolves it, and `link_badge_to_employee` is the
	deliberate act that populates that register — a phone that could name the
	picker directly would be able to move somebody else's piece-rate onto its own
	badge without ever touching the map an operator reads.

	THE TIMESTAMP IS CONVERTED, NOT PASSED THROUGH — v0.59.2, AND IT IS WHY NOT
	ONE BUCKET ENTRY HAD EVER SYNCED FROM A HANDSET. `BadgeAPI.payload` stamps
	every capture with an `ISO8601DateFormatter` set to `.withInternetDateTime`
	in UTC, so the wire carries `2026-08-11T07:12:00Z`. Bucket Log Entry's
	`timestamp` is a Frappe `Datetime`, which is a MariaDB DATETIME, which
	answers that string with `OperationalError (1292, "Incorrect datetime
	value")`. The failure was invisible from both ends: `validate_bucket_entry`
	APPROVED the string (`bucket_bridge._parse_dt` splits the `T` and drops the
	`Z` quite happily), so the entry got past every check this app makes and
	then died at the insert, and the whole batch came back a 500 — the same
	shape as v0.59.1's model pull, at the other boundary where something that
	speaks JSON writes a timestamp into a Datetime column.

	A value that will not convert is handed back UNCHANGED rather than blanked.
	`as_mariadb_datetime` answers `""` for anything unreadable, and a blank
	timestamp reaches the validator as "timestamp is required", which tells the
	phone the field was missing when in fact it was unreadable. Passing the
	original through gets the entry the message that names the value.

	`capture_mode` AND `auto_verdict` ARE SENT AND ARE DROPPED HERE, KNOWINGLY.
	`BadgeAPI.payload` writes both on every row so the farm can answer "how many
	of this season's buckets did a model actually look at" — `auto_verdict` is
	`BucketEntry.AutoVerdict` (full/not_full/manual_override/timeout/
	manual_tally) and `capture_mode` is the "Badge Only" / "ML Verified" split
	derived from it. Bucket Log Entry has no column for either, so they are not
	read: forwarding a key the doctype has no field for would be dropped by
	Frappe anyway, one layer further in and without this note. Neither is an
	input to pay — the verdict is the only field that decides that — so nothing
	is owed a picker while they are unstored. Adding the two fields is a
	doctype change with a patch behind it, deliberately not bundled with a
	datetime fix.

	The rest is a rename. `FarmOpsKit/Capture/BucketEntry.swift` encodes `id`,
	`session_id`, `badge_id` and `accepted`; the doctype's columns are
	`entry_uuid`, `session_uuid`, `worker_badge` and `verdict`. Both spellings are
	accepted so a Desk-shaped payload and the handset's own both work, and
	`accepted` is translated to the Select's two options rather than passed
	through — the app has a boolean and the register has words.
	"""
	if raw in (None, ""):
		raise ToolError("entries is required — a sync with nothing in it is not a sync.")
	if isinstance(raw, dict):
		raw = [raw]
	if not isinstance(raw, list):
		raise ToolError(
			'entries must be a list of objects like {"entry_uuid": "...", "verdict": "Accepted"}.'
		)
	if not raw:
		raise ToolError("entries is required — a sync with nothing in it is not a sync.")
	if len(raw) > BUCKET_BATCH_CAP:
		raise ToolError(
			f"{len(raw)} captures is more than one sync call accepts ({BUCKET_BATCH_CAP}). Send the "
			"queue in slices. Nothing was changed."
		)

	out = []
	for index, entry in enumerate(raw):
		if not isinstance(entry, dict):
			raise ToolError(f"entries[{index}] must be an object.")
		verdict = str(entry.get("verdict") or "").strip()
		if not verdict and entry.get("accepted") is not None:
			verdict = "Accepted" if entry.get("accepted") in (True, 1, "1", "true", "True") else "Rejected"
		# ISO 8601 in, MariaDB DATETIME out. See the docstring: the raw value is
		# kept when it will not convert, so the refusal names the value rather
		# than reporting a field the phone did send as missing.
		timestamp = entry.get("timestamp")
		row = {
			"company": company,
			"entry_uuid": str(entry.get("entry_uuid") or entry.get("id") or "").strip(),
			"session_uuid": str(entry.get("session_uuid") or entry.get("session_id") or "").strip(),
			"worker_badge": str(entry.get("worker_badge") or entry.get("badge_id") or "").strip(),
			"timestamp": datetimes.as_mariadb_datetime(timestamp) or timestamp,
			"verdict": verdict,
		}
		for key in ("coverage_percent", "gps_lat", "gps_lon"):
			if entry.get(key) not in (None, ""):
				row[key] = entry[key]
		for key in ("model_uuid", "h3_cell", "device_id"):
			value = str(entry.get(key) or "").strip()
			if value:
				row[key] = value
		out.append(row)
	return out


def _full_name(first, last, given) -> str:
	"""One `employee_name`, from whichever halves the wizard filled in.

	`OnboardingIdentity.employeePayload` already joins the two and sends all three
	keys, so this is the fallback for a client that sends only the halves rather
	than the usual path. The one-word case is NOT patched over here: a record
	carrying "Rosa" and nothing else names nobody findable on an I-9, a payroll
	register or a dispatch board, and `create_employee` refuses it with that
	sentence. Composing something plausible instead would put the refusal off
	until the person had filled in four more screens.
	"""
	whole = str(given or "").strip()
	if whole:
		return whole
	return " ".join(part for part in (str(first or "").strip(), str(last or "").strip()) if part)


def _employee_identity(name: str) -> dict:
	"""The two facts the wizard holds on to after step 1, read back off the record.

	`OnboardingAPI.CreatedEmployee` decodes `name` with `try c.decode(String.self)`
	— absent or null and the whole row throws, mid-flow, on a person who has just
	been hired — and `employee_id` as an optional it falls back to the docname for.

	`employee_id` IS `employee_number` AND IS OFTEN EMPTY. Frappe HR's Employee
	carries the docname as its identity and `employee_number` as the payroll number
	an operator may or may not keep; a site that keeps none gets null here rather
	than a docname echoed into a second key, because the app already writes that
	fallback itself and a server that guessed would hide which sites actually
	number their people.
	"""
	row = (
		frappe.db.get_value(
			EMPLOYEE,
			name,
			compat.existing_fields(EMPLOYEE, ["employee_name", "employee_number", "company", "status"]),
			as_dict=True,
		)
		or {}
	)
	return {
		"name": name,
		"employee_id": str(row.get("employee_number") or "") or None,
		"employee_name": row.get("employee_name"),
		"company": row.get("company"),
		"status": row.get("status"),
	}


def _crew(raw, allowed: list) -> list:
	"""The crew a foreman rostered when opening a shift, each name checked here.

	`shifts._crew_argument` accepts docnames, employee ids and names and resolves
	them, and `start_shift` then refuses any crew member employed by another
	entity. Both still run. What this adds is the check the mobile surface always
	adds and the tool layer deliberately does not: a name that resolves to an
	Employee of an entity THIS CALLER cannot reach reads as not found, so a phone
	cannot enumerate the holding company's payroll by watching which names roster.

	A bare string, a comma-joined string and a list of `{"employee", "role"}`
	objects all arrive in practice — the handset sends the last of those — so all
	three are normalised here rather than argued about downstream.
	"""
	if raw in (None, "", []):
		return []
	if isinstance(raw, str):
		raw = [part.strip() for part in raw.split(",") if part.strip()]
	if isinstance(raw, dict):
		raw = [raw]
	if not isinstance(raw, list):
		raise ToolError('crew_employees must be a list of employees, or of objects like {"employee": "..."}.')

	out = []
	for index, entry in enumerate(raw):
		if isinstance(entry, dict):
			person = entry.get("employee") or entry.get("name")
			role = str(entry.get("role") or "").strip()
			joined = str(entry.get("joined_at") or "").strip()
		else:
			person, role, joined = entry, "", ""
		if not str(person or "").strip():
			raise ToolError(f"crew_employees[{index}] names nobody.")
		row = {"employee": _employee_argument(person, allowed, f"crew_employees[{index}]")}
		if role:
			row["role"] = role
		if joined:
			row["joined_at"] = joined
		out.append(row)
	return out


def _model_docname(model) -> str:
	"""`model` resolved to an ML Model docname before `guard.require_scoped_doc`
	checks it, so a phone naming the `uuid` off its own cached manifest — which
	is `source_uuid`, not a docname, see `model_registry.build_model_manifest`
	— still resolves to something the scoping check can run against.

	A value that resolves to nothing is returned unchanged: `require_scoped_doc`
	then does its own `frappe.db.exists` and answers the same 404 it would for
	any other docname nobody has heard of.
	"""
	value = str(model or "").strip()
	if not value or frappe.db.exists(ML_MODEL, value):
		return value
	return str(frappe.db.get_value(ML_MODEL, {"source_uuid": value}, "name") or value)


def _previous_assignment(employee: str, allowed: list) -> dict | None:
	"""Where this person slept last season, and whether that cabin is free now.

	v0.54.0, for the wizard's "Last year: MC-Cabin-07" row. A returning picker
	who had the same cabin for three seasons is one tap instead of a scroll
	through forty units nobody remembers the numbers of, and the foreman does not
	have to ask somebody where they slept last August.

	ENDED ASSIGNMENTS ONLY, MOST RECENT FIRST. A returning worker is by definition
	somebody whose last stay finished. An open assignment means they are housed
	right now, which is a different screen and a different sentence — offering
	"last year: Cabin 7" to somebody who is currently IN Cabin 7 is an offer to
	double-book them — so it is reported as `currently_housed` and no preference
	is returned.

	AVAILABILITY IS COMPUTED FOR THE UNIT ITSELF rather than read off the list
	this is returned beside. That list is filtered — by branch, and by the default
	that drops full and condemned units — so a cabin missing from it is precisely
	the case this has to answer for, and looking it up there would report every
	full cabin as available.

	A UNIT OR AN ASSIGNMENT BELONGING TO AN ENTITY THE CALLER CANNOT REACH IS NOT
	REPORTED AT ALL. `guard.scoped` cannot do it — a Housing Unit calls its
	company `owning_entity` — so the check is here, and it is the same rule
	`assign_housing` applies by hand for the same reason.
	"""
	if not compat.doctype_exists(HOUSING_ASSIGNMENT):
		return None

	# AN OPEN ASSIGNMENT IS CHECKED FIRST, AND IT WINS OVER ANY ENDED ONE. Not
	# merely a fallback for somebody with no history: a worker who had Cabin 7
	# last season and is in Cabin 3 tonight has both, and answering with Cabin 7
	# would offer a one-tap re-assignment for somebody who already has a bed. What
	# they need is "they are already housed", which is true regardless of how many
	# finished seasons sit behind it.
	housed = frappe.db.get_all(
		HOUSING_ASSIGNMENT,
		filters={"employee": employee, "end_date": ("is", "not set")},
		fields=["name", "unit", "assigned_date"],
		order_by="assigned_date desc",
		limit_page_length=1,
	)
	if housed:
		current = dict(housed[0])
		if not _unit_is_reachable(str(current.get("unit") or ""), allowed):
			return None
		return {
			"assignment": current.get("name"),
			"unit": current.get("unit"),
			"unit_name": _unit_label(str(current.get("unit") or "")),
			"check_in_date": str(current.get("assigned_date") or "") or None,
			"check_out_date": None,
			"currently_housed": True,
			"available": False,
			"unavailable_reason": (
				"This is where they are housed now, not where they were. Ending that "
				"assignment is end_housing_assignment; nothing here re-assigns anybody."
			),
		}

	rows = frappe.db.get_all(
		HOUSING_ASSIGNMENT,
		filters={"employee": employee, "end_date": ("is", "set")},
		fields=["name", "unit", "assigned_date", "end_date", "housing_deduction_from_wages"],
		order_by="end_date desc, assigned_date desc",
		limit_page_length=1,
	)
	if not rows:
		# A first-season hire. Not an error and not a warning — just nothing to
		# put at the top of the list.
		return None

	row = dict(rows[0])
	unit = str(row.get("unit") or "")
	if not _unit_is_reachable(unit, allowed):
		return None

	capacity, occupants, condition = _unit_occupancy(unit)
	condemned = condition == "Uninhabitable"
	full = bool(capacity) and occupants >= capacity
	reason = None
	if condemned:
		reason = "Marked Uninhabitable since they left. It has to be repaired and inspected first."
	elif full:
		reason = f"All {capacity} bed(s) are taken."

	return {
		"assignment": row.get("name"),
		"unit": unit,
		"unit_name": _unit_label(unit),
		"check_in_date": str(row.get("assigned_date") or "") or None,
		"check_out_date": str(row.get("end_date") or "") or None,
		"currently_housed": False,
		"capacity": capacity or None,
		"current_occupants": occupants,
		"open_beds": max(0, capacity - occupants) if capacity else None,
		"available": not (condemned or full),
		"unavailable_reason": reason,
		"housing_deduction_from_wages": row.get("housing_deduction_from_wages") or "Unknown",
	}


#: Which registers a handset may append narrative to, and the role each needs.
#: `None` means any enrolled worker: a Farm Task's narrative is the account of
#: work somebody did, and refusing a picker their own account of their own job
#: would be refusing the thing this feature is for. The two personnel registers
#: are HR's, and the accident register is open to write because the person who
#: finds somebody on the ground is whoever finds them.
#: The handset's word for a report's direction, mapped onto the column's own.
#:
#: v0.98.0, ITEM 12. `Farm Incident Record.report_direction` has carried
#: `Supervisor Report` / `Worker Report` since v0.94.0 and the tool has enforced
#: every rule that follows from it — a Worker Report takes no `discipline_type`,
#: gets no rung on the escalation chain, and is NOT refused after a termination,
#: which is the one report that must never be refused on those grounds. None of
#: that was reachable in the app's vocabulary, so a grievance was being filed as
#: `create_discipline_record` at the lowest step with `DISPUTE RAISED BY …`
#: typed into the description. That record IS a warning: it sits on a
#: progressive-discipline chain, in the file of the person who complained.
#:
#: TWO WORDS FOR ONE COLUMN RATHER THAN A SECOND COLUMN. The plan allowed either
#: a `direction` field or a `create_dispute`; a second Select saying the same
#: thing in different words is the table sprawl this codebase is built against,
#: and would leave two columns to disagree about one record. The stored value
#: stays the doctype's, so the Desk, the chain query and every report already
#: written keep reading one vocabulary.
REPORT_DIRECTIONS = {
	"disciplinary": discipline_tools.SUPERVISOR_REPORT,
	"grievance": discipline_tools.WORKER_REPORT,
	"dispute": discipline_tools.WORKER_REPORT,
}


NARRATIVE_TARGETS = {
	FARM_TASK: None,
	"Accident Report": None,
	"Farm Incident Record": "hr",
}


def _narrative_target(user: str, doctype, name, task, allowed: list) -> tuple:
	"""`(doctype, docname)` for a narrative call, scoped and role-checked.

	THE ALLOWLIST IS SHORTER THAN THE TOOL'S. `tools/narrative.py` will append to
	any of its three parents; this surface adds the role gate on the one that is
	a personnel document, because a discipline record's narrative is the account
	of a disciplinary meeting and a field credential has no business in it.
	"""
	target = str(doctype or "").strip()
	docname = str(name or "").strip()
	if not target and task:
		target, docname = FARM_TASK, str(task).strip()
	if target not in NARRATIVE_TARGETS:
		frappe.throw(
			f"{target or '(none)'} does not carry a narrative on this surface. The registers that "
			f"do are: {', '.join(sorted(NARRATIVE_TARGETS))}.",
			frappe.PermissionError,
		)
	if NARRATIVE_TARGETS[target] == "hr":
		personnel.require_hr_role()
	return target, guard.require_scoped_doc(target, docname, "name", allowed)


def _report_direction(given, label: str = "report_direction") -> str:
	"""One `report_direction` value, from either vocabulary, or "".

	ACCEPTS THE COLUMN'S OWN WORDS AND THE HANDSET'S, case-insensitively, and
	refuses anything else BY NAME with both lists in the sentence. A refusal that
	only says "invalid" leaves a client author guessing between four spellings,
	which is how `DISPUTE RAISED BY …` got typed into a description in the first
	place.
	"""
	wanted = str(given or "").strip()
	if not wanted:
		return ""
	folded = wanted.lower()
	if folded in REPORT_DIRECTIONS:
		return REPORT_DIRECTIONS[folded]
	for option in discipline_tools.DIRECTIONS:
		if option.lower() == folded:
			return option
	frappe.throw(
		f"{label} {wanted!r} is not one this surface knows. The stored values are "
		f"{' and '.join(discipline_tools.DIRECTIONS)}; "
		f"{', '.join(sorted(REPORT_DIRECTIONS))} are accepted as spellings of them. "
		"Nothing was created.",
		frappe.ValidationError,
	)


def _caller_language(user: str, employee: str = "") -> str:
	"""The language this person reads, or "". Never guessed from a device locale.

	A phone set to English by whoever handed it over says nothing about who is
	holding it now — see `Employee.preferred_language`, which this reads. An
	empty answer is a real one and is left empty rather than defaulted, so a
	narrative entry says "nobody told me" rather than "English".

	DELIBERATELY NOT `_response_language` BELOW, and the difference is the whole
	point of having two functions. This one TAGS A RECORD — `source_language` on
	a voice note is a claim about what language somebody spoke, and an
	`Accept-Language` header is not evidence of that. The other one CHOOSES A
	RENDERING, where the header is a reasonable guess and a wrong guess costs
	somebody a re-read rather than a mislabelled personnel record.
	"""
	try:
		return wizard_tools.preferred_language(user, employee)
	except Exception:  # pragma: no cover - a site without the column
		return ""


def _response_language(language=None, employee: str = "") -> tuple:
	"""`(code, source)` — what language to RENDER this response in. Never raises.

	`Employee.preferred_language` first, `Accept-Language` second, English last.
	`guard._stamp_language` already did this work at the top of the request, so
	this normally just reads the answer back off `frappe.local` — the parameters
	are for the two cases where it cannot: a caller naming a language explicitly
	(an operator previewing Spanish), and an employee whose record the guard did
	not resolve because the endpoint had not looked it up yet.

	See `tools/translations.py` for why the header LOSES to the column, and why
	the source is returned rather than discarded: "why is this worker seeing
	English" is a support question with exactly four possible answers.
	"""
	try:
		from ..tools import translations

		explicit = translations.normalize_language(str(language or ""))
		if explicit:
			return explicit, "explicit"
		if employee:
			stated = translations.normalize_language(translations.preferred_language(employee=employee))
			if stated:
				return stated, "employee"
		established = guard.caller_language()
		if established:
			return established, guard.caller_language_source() or "default"
		return translations.DEFAULT_LANGUAGE, "default"
	except Exception:  # pragma: no cover - the language must never break a call
		return "en", "default"


def _unit_is_reachable(unit: str, allowed: list) -> bool:
	"""Does this Housing Unit belong to an entity this caller may see?

	A blank owning entity is reachable, matching `guard.scoped`'s rule: a record
	with no company is a data problem rather than another entity's secret, and
	hiding it makes it invisible instead of fixed.
	"""
	if not unit or not frappe.db.exists(HOUSING_UNIT, unit):
		return False
	owner = str(frappe.db.get_value(HOUSING_UNIT, unit, "owning_entity") or "")
	return not owner or owner in set(allowed or [])


def _unit_label(unit: str) -> str | None:
	"""What is painted on the door, rather than the docname with the parcel key on it."""
	if not unit:
		return None
	return str(frappe.db.get_value(HOUSING_UNIT, unit, "unit_name") or "") or unit


def _unit_occupancy(unit: str) -> tuple:
	"""`(capacity, occupants_today, condition)` for one unit.

	Occupancy is counted through `housing.occupancy_for`, the same overlap rule
	`assign_housing` refuses on, so "available" here and "accepted" there cannot
	come to different answers about the same cabin on the same day.
	"""
	row = frappe.db.get_value(HOUSING_UNIT, unit, ["capacity", "condition"], as_dict=True) or {}
	today = frappe.utils.today()
	occupants = housing_tools.occupancy_for(unit, today, today)
	return int(row.get("capacity") or 0), len(occupants), str(row.get("condition") or "")


# ── 1. get_current_user_context ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_current_user_context", limit=guard.READ_LIMIT)
def get_current_user_context(user: str) -> dict:
	"""Who is calling, what they may do, and which entity the app opens on.

	Doubles as credential validation: the app calls it immediately after a scan
	and on every manual refresh, and treats a 401 as "this credential is dead,
	sign out and re-scan". Frappe answers the 401 itself when the token is bad,
	so this only ever runs for a credential that already checked out.
	"""
	guard.require_scope(user)
	result = mobile_tools.get_current_user_context({})
	return shape.user_context(result.data, user)


# ── 2. list_my_tasks ────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_my_tasks", limit=guard.READ_LIMIT)
def list_my_tasks(user: str, company=None, timezone=None) -> dict:
	"""What this worker is holding right now: claimed and in progress.

	`timezone` is optional and affects DISPLAY only: every timestamp keeps its
	stored spelling and gains a `*_local` twin rendered in the zone named here,
	or in the site's own when it is not. An unknown zone is refused rather than
	quietly answered in UTC — see `erpnext_mcp/timezones.py`.
	"""
	allowed = guard.require_scope(user)
	wanted = guard.require_company(user, company, allowed)

	clock = timezones.Renderer({"timezone": timezone} if timezone else {})
	result = fieldwork.list_my_tasks({"company": wanted} if wanted else {})
	rows = []
	for entry in result.data.get("assignments") or []:
		detail = entry.get("task_detail")
		if detail:
			rows.append(shape.task(detail, entry, clock))
	rows = guard.scoped(rows, allowed)
	return {"tasks": rows, "count": len(rows), "company": wanted or None, **clock.block()}


# ── 3. list_available_tasks ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_available_tasks", limit=guard.READ_LIMIT)
def list_available_tasks(user: str, company=None) -> dict:
	"""The pool this worker could take from.

	COMPANY IS ADVISORY AND THE SERVER FILTERS ANYWAY. The app's own contract
	asks for this in as many words — "a client that sends nothing must not
	receive everything" — and `list_available_tasks` in the tool layer reads
	through `frappe.db.get_all`, which does NOT consult User Permissions. So with
	no company argument the pool is fetched once per accessible entity rather
	than once unfiltered, and `guard.scoped` checks the result again on the way
	out.
	"""
	allowed = guard.require_scope(user)
	wanted = guard.require_company(user, company, allowed)

	rows, may_claim, remaining = [], True, None
	for entity in [wanted] if wanted else allowed:
		result = fieldwork.list_available_for_me({"company": entity})
		rows.extend(shape.tasks(result.data.get("tasks") or []))
		if result.data.get("may_claim") is False:
			may_claim = False
		claims = result.data.get("claims_remaining")
		if claims is not None:
			remaining = claims if remaining is None else min(remaining, claims)

	seen, unique = set(), []
	for row in guard.scoped(rows, allowed):
		if row.get("name") in seen:
			continue
		seen.add(row.get("name"))
		unique.append(row)
	return {
		"tasks": unique,
		"count": len(unique),
		"company": wanted or None,
		"may_claim": may_claim,
		"claims_remaining": remaining,
	}


# ── 4. get_task ─────────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_task", limit=guard.READ_LIMIT)
def get_task(user: str, task=None, timezone=None) -> dict:
	"""One task in full: the job, the contract, and why it exists.

	v0.76.0. A FINISHED TASK IS SHAPED AGAINST THE ASSIGNMENT THAT FINISHED IT.
	`live_assignment` is Claimed-or-In-Progress by definition, so a task that has
	been completed has no live one and used to come back with every assignment
	field null — no `started_at`, and no `completed_at` for the app to stop its
	timer against. `FarmTask.elapsedMinutes` then counted from nothing to now,
	and a job closed at four in the afternoon read as eleven hours' work when
	somebody opened it the next morning. The completion is right there in the
	history; this picks it up when there is no live assignment to prefer.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_TASK, task, "task", allowed)

	clock = timezones.Renderer({"timezone": timezone} if timezone else {})
	result = fieldwork.get_task_with_evidence_contract({"task": name})
	data = result.data
	out = shape.task(
		data.get("task") or {},
		data.get("live_assignment") or _last_completed(data.get("history")),
		clock,
	)
	out.update(clock.block())
	out["is_mine"] = data.get("is_mine")
	out["evidence_contract"] = data.get("evidence_contract")
	out["evidence_outstanding"] = data.get("evidence_outstanding")
	out["evidence_complete"] = data.get("evidence_complete")
	return out


# ── 5. claim_task ───────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("claim_task", limit=guard.WRITE_LIMIT, mutating=True)
def claim_task(user: str, task=None) -> dict:
	"""Take one task from the pool.

	Never queued offline by the app, and it must not be: two workers offline
	would both believe they own the same cabin, and the concurrent-claim limit
	cannot be enforced from a phone.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_TASK, task, "task", allowed)

	result = fieldwork.claim_task_via_mobile({"task": name})
	# v0.18.2: dispatch.claim_farm_task spreads task fields at the TOP LEVEL of
	# data (see dispatch.py `_describe_task(dict(task_doc.as_dict()))` inside
	# `data={**_describe_task(...), "assignment": ..., ...}`), not nested under
	# a "task" key like start_farm_task does. shape.task(data.get("task") or {})
	# passed an empty dict and emitted {"name": null, ...}, which crashed the
	# iOS Codable decoder with "Bad value at 'name'". Extract the task fields
	# out of the flat response instead of asking for a "task" wrapper that
	# isn't there.
	task_row = {
		key: value
		for key, value in result.data.items()
		if key
		not in (
			"assignment",
			"concurrent_claims",
			"claims_remaining",
			"evidence_you_will_need",
			"me",
			"next",
		)
	}
	return shape.task(task_row, result.data.get("assignment") or {})


# ── 6. start_task ───────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("start_task", limit=guard.WRITE_LIMIT, mutating=True)
def start_task(user: str, task=None, task_assignment=None) -> dict:
	"""Clock in on one claimed task. `started_at` is what duration counts from."""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_TASK, task, "task", allowed)
	assignment = _assignment(name, task_assignment, allowed)

	inner = {"task": name}
	if assignment:
		inner["assignment"] = assignment
	result = fieldwork.start_task_via_mobile(inner)
	return shape.task(result.data.get("task") or {}, result.data.get("assignment") or {})


# ── 7. complete_task_via_mobile ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("complete_task_via_mobile", limit=guard.COMPLETE_LIMIT, mutating=True)
def complete_task_via_mobile(
	user: str,
	task=None,
	task_assignment=None,
	findings_text=None,
	completion_narrative=None,
	completed_at=None,
	actual_duration_minutes=None,
	clean_pass=None,
	witness=None,
	latitude=None,
	longitude=None,
	farm_location_gps=None,
	evidence_files=None,
	visit_id=None,
	observation_type=None,
	growth_stage_code=None,
	brix_reading=None,
	brix_method=None,
) -> dict:
	"""Finish one task: file the evidence, write the compliance record.

	THE ARGUMENTS ARE LISTED RATHER THAN FORWARDED. Frappe hands a whitelisted
	method whatever keys the body carried, so `**kwargs` here would pass the
	phone's JSON straight into `complete_farm_task` — including `record_data`,
	which writes arbitrary fields into the compliance record, and `worker_id`,
	which names whose completion it is. Naming every accepted argument is what
	makes those two unreachable.

	`findings_text` is passed THROUGH THE PRESENCE TEST, not through truthiness.
	An empty string is a positive statement — "I looked and nothing was wrong" —
	and the tool layer distinguishes it from the argument being absent, which
	records that nobody was asked.

	`latitude`/`longitude` NOW REACH THE RECORD. v0.19.1 added
	`farm_location_gps` to Farm Task Assignment, so the pair the shipped app has
	been sending since v0.18 stops being audit-row-only and becomes the location
	half of FSMA §112.161(a)(1)(i). An explicit `farm_location_gps` wins — a
	worker who typed "MC-Cabin-01" in a shed with no fix said something the
	handset could not — and the coordinates are formatted only as a fallback.
	Both still land in the audit row either way, unchanged.

	v0.20.1. THIS CALL IS SAFE TO SEND TWICE, which it was not before. A queued
	completion that reached the server and whose acknowledgement did not reach
	the handset used to come back as a hard error about work that was already
	filed — three Failed entries per task in a sync queue, on an iPad, over an
	evening's real work. `complete_farm_task` now recognises an identical
	resubmission by its signature and answers with the completion already on
	record and `x_idempotent: true`. IT IS STILL A REFUSAL when the second
	submission is a different one: a different worker, different evidence or a
	different account of the work is a conflict, and absorbing it silently would
	be a worse bug than the one being fixed.

	`visit_id` IS THE HANDSET'S, forwarded as sent. It groups the completions of
	one trip so `list_visits` can report the trip rather than five unrelated
	tasks. Its shape IS checked, one layer down where the column is written: a
	UUID as 8-4-4-4-12, either case, or the call is refused with the format in
	the message. Omitting it is not an error — it files the completion outside
	any visit, which `list_visits` counts separately and says so.

	v0.117.0. THE FOUR SCOUTING MEASUREMENTS ARE NAMED ARGUMENTS AND
	`record_data` IS STILL NOT ONE. `SERVER_CHANGES.md` §26: the paragraph above
	is why the phone cannot send `record_data`, and the consequence was that a
	scouting round closed from the app filed an observation carrying the seeded
	template's defaults with `brix_reading` and `growth_stage_code` both null —
	on a round where somebody read both. Every one of these four is already in
	`scouting.PAYLOAD_FIELDS`, so nothing new became writable: what arrives is a
	closed list of four measurement columns rather than an open dictionary, and
	the reasoning that made `record_data` and `worker_id` unreachable is intact.
	`_measurements` refuses a bad one HERE, while the scout is still in the
	block — see it for why that is not the sweep's job to discover on Friday.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_TASK, task, "task", allowed)
	assignment = _assignment(name, task_assignment, allowed)

	inner = {"task": name}
	if assignment:
		inner["assignment"] = assignment
	if findings_text is not None:
		inner["findings_text"] = findings_text
	if clean_pass is not None:
		inner["clean_pass"] = clean_pass
	for key, value in (
		("completion_narrative", completion_narrative),
		("witness", witness),
		("completed_at", completed_at),
		("actual_duration_minutes", actual_duration_minutes),
		("visit_id", visit_id),
	):
		if value is not None:
			inner[key] = value
	location = _location(farm_location_gps, latitude, longitude)
	if location:
		inner["farm_location_gps"] = location
	evidence = _evidence(evidence_files)
	if evidence:
		inner["evidence_files"] = evidence
	measurements = _measurements(name, observation_type, growth_stage_code, brix_reading, brix_method)
	if measurements:
		inner["record_data"] = measurements

	result = fieldwork.complete_task_via_mobile(inner)
	return shape.completion(result.data)


# ── 8. reject_task ──────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("reject_task", limit=guard.WRITE_LIMIT, mutating=True)
def reject_task(user: str, task=None, task_assignment=None, reason=None) -> dict:
	"""Hand one task back to the pool, with a reason that goes on the record.

	`cancel` IS NOT ACCEPTED. `reject_farm_task` takes it, and it cancels the
	task outright instead of returning it to the pool — a worker saying "I could
	not do this" must not be able to make the work disappear, so the argument
	stops here and the task always goes back.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_TASK, task, "task", allowed)
	assignment = _assignment(name, task_assignment, allowed)
	if not str(reason or "").strip():
		frappe.throw(
			"A reason is required to hand a task back. 'The ladder is broken and I could not "
			"reach the detector' is a fact somebody can act on; an empty rejection is not.",
			frappe.ValidationError,
		)

	inner = {"task": name, "reason": str(reason).strip(), "worker_id": _employee(user), "cancel": False}
	if assignment:
		inner["assignment"] = assignment
	result = dispatch.reject_farm_task(inner)
	return {
		"task": (result.data.get("task") or {}).get("name"),
		"returned_to_state": result.data.get("returned_to_state"),
		"reason": str(reason).strip(),
	}


# ── 10. report_field_task ───────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("report_field_task", limit=guard.WRITE_LIMIT, mutating=True)
def report_field_task(
	user: str,
	location_doctype=None,
	location=None,
	task_type=None,
	skill_required=None,
	urgency=None,
	description=None,
	photo_file_token=None,
	asset=None,
	affected_asset=None,
	affected_block=None,
	observed_at=None,
	estimated_duration_minutes=None,
) -> dict:
	"""A worker in the field flags a problem on the spot.

	THE FOUR STRUCTURED ARGUMENTS AND THE ESTIMATE ARE v0.98.0, ITEM 5, and each
	is declared here because this transport's argument filter delivers only what
	a signature names — a key the wrapper omits is not passed through, it is
	dropped, and the report is filed without it and says nothing about the loss.

	`affected_asset` is `asset` under the name the app's form uses;
	`affected_block` is a `Field` docname that fills the location pair when no
	explicit pair was sent; `observed_at` is when the thing was SEEN, which is
	its own column and is not `reported_at` — see `_structured_report`.

	`reported_by` IS STILL NOT ON THIS SIGNATURE. It is the authenticated caller,
	and the whole reason is in the paragraph below. `create_farm_task` accepts one
	because a foreman raising work is recording somebody else's observation; a
	worker reporting is recording their own.

	THE FIELD REPORT IS THE WORK ORDER. The worker taps, snaps a photo,
	describes the problem, and the task is in the pool — one act, not a
	two-step process with a separate ticket doctype between them.

	`reported_by` IS FILLED FROM THE AUTHENTICATED CALLER, not from the
	body. An account that can name somebody else in a request body is not
	scoped to anything — the same principle as `list_my_tasks` filling
	`worker_id` from the session.

	`urgency` is CAPPED: field workers may choose Normal or High. Critical
	is restricted to Foreman and Farm Manager roles, and the tool enforces
	that — the wrapper passes the value through because the tool layer
	already has the role check.
	"""
	allowed = guard.require_scope(user)
	employee = _employee(user)

	inner = {"reported_by": employee}
	if location_doctype:
		inner["location_doctype"] = str(location_doctype).strip()
	if location:
		inner["location"] = str(location).strip()
	if task_type:
		inner["task_type"] = str(task_type).strip()
	if skill_required:
		inner["skill_required"] = str(skill_required).strip()
	if urgency:
		inner["urgency"] = str(urgency).strip()
	if description:
		inner["description"] = str(description).strip()
	if photo_file_token:
		inner["photo_file_token"] = str(photo_file_token).strip()
	named_asset, _ = _one_spelling(affected_asset, asset, "affected_asset", "asset")
	if named_asset:
		inner["asset"] = named_asset
	for key, value in (
		("affected_block", affected_block),
		("observed_at", observed_at),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()
	if estimated_duration_minutes is not None:
		# UNPARSED, like every other number on this surface: `as_int` is what
		# parses it inside the tool, and an `int()` here would 500 on a body that
		# sent "twenty" instead of refusing it in a sentence.
		inner["estimated_duration_minutes"] = estimated_duration_minutes

	company = guard.require_company(user, inner.get("company"), allowed) if inner.get("company") else ""
	if not company and allowed:
		inner["company"] = allowed[0]
	elif company:
		inner["company"] = company

	result = dispatch.report_field_task(inner)
	data = result.data
	return shape.task(data, None)


# ── 11. list_compliance_alerts ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_compliance_alerts", limit=guard.READ_LIMIT)
def list_compliance_alerts(user: str, company=None) -> dict:
	"""Open compliance alerts for the entities this caller may reach. View-only.

	THE ROLE GATE IS NOT THE APP'S. `UserContext.canViewCompliance` hides the
	Compliance tab from a picker, and the app's own contract says in as many
	words that this is UI courtesy rather than the security boundary. The gate
	that matters is `guard.endpoint` plus the entity scoping below, both of which
	run whatever the app decided to draw.
	"""
	allowed = guard.require_scope(user)
	wanted = guard.require_company(user, company, allowed)

	inner = {"company": wanted} if wanted else {}
	result = fieldwork.list_compliance_calendar_for_me(inner)
	rows = guard.scoped(shape.alerts(result.data.get("alerts") or []), allowed)
	return {
		"alerts": rows,
		"count": len(rows),
		"company": wanted or None,
		"critical": len([row for row in rows if row.get("severity") == "Critical"]),
		"overdue": len([row for row in rows if row.get("overdue")]),
	}


# ── 11a. dismiss_compliance_alert ───────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("dismiss_compliance_alert", limit=guard.WRITE_LIMIT, mutating=True)
def dismiss_compliance_alert(user: str, alert=None, reason=None) -> dict:
	"""Close one alert the SERVER marked closable, with a reason. v0.57.0.

	`API_CONTRACT.md` §8.2, and the gate is the whole of it: this refuses any
	alert whose `can_dismiss` is not set, which is every alert until somebody
	says otherwise about that one.

	THE APP'S CHECK IS UI COURTESY AND THIS IS THE BOUNDARY, which the app's own
	contract asks for in as many words. The Dismiss button appears only where
	`list_compliance_alerts` sent `can_dismiss: true`; the refusal below is what
	happens when something posts anyway.

	WHY A PHONE MAY NOT SIMPLY DISMISS. An overdue housing inspection is not a
	notification. Waving one off from a handset leaves a cabin uninspected and
	the compliance calendar quiet about it, which is why the mobile surface
	shipped with no dismiss at all — and why the alerts that genuinely are stale
	are marked one at a time, by somebody who can see the whole picture, rather
	than by whoever is holding the phone.

	THE REASON IS NOT DECORATION. It is the entire audit trail for an obligation
	nobody discharged. Empty is refused here as well as on the handset, exactly
	as `reject_task`'s is, and `tools/calendar.py` refuses a word where a
	sentence belongs.

	NOTHING ABOUT THE UNDERLYING RECORD CHANGES. The certificate is still
	expired, the cabin still uninspected. What is recorded is that somebody with
	a phone in an orchard decided it did not need doing, and who they were.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(ALERT, alert, "alert", allowed)
	if not str(reason or "").strip():
		frappe.throw(
			"A reason is required to dismiss a compliance alert. It is the only part of this "
			"record nobody can reconstruct: the alert itself the nightly sweep can rebuild from "
			"the source record, but why somebody decided an obligation did not need meeting "
			"exists nowhere else.",
			frappe.ValidationError,
		)

	result = compliance_calendar.dismiss_compliance_alert({"alert": name, "reason": str(reason).strip()})
	data = result.data
	return {
		"alert": data.get("name"),
		"dismissed": bool(data.get("dismissed")),
		"dismissed_by": data.get("dismissed_by"),
		"dismissed_on": data.get("dismissed_on"),
		"reason": data.get("dismissed_reason"),
	}


# ── 12. scan_asset ────────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("scan_asset", mutating=True, limit=guard.WRITE_LIMIT)
def scan_asset(user: str, asset_name=None, gps_lat=None, gps_lon=None) -> dict:
	"""Record a scan event on an asset tag. Returns asset detail + open tasks."""
	guard.require_scope(user)
	asset_name = str(asset_name or "").strip()
	if not asset_name:
		frappe.throw("asset_name is required.", frappe.ValidationError)

	result = asset_tags.scan_asset(
		{
			"asset_name": asset_name,
			"scanned_by": user,
			"gps_lat": gps_lat,
			"gps_lon": gps_lon,
		}
	)
	return result.data


# ── 13. get_asset_detail ──────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_asset_detail", limit=guard.READ_LIMIT)
def get_asset_detail(user: str, asset_name=None) -> dict:
	"""Asset detail screen data: current state, open tasks, history."""
	guard.require_scope(user)
	asset_name = str(asset_name or "").strip()
	if not asset_name:
		frappe.throw("asset_name is required.", frappe.ValidationError)

	result = asset_tags.get_asset_detail({"asset_name": asset_name})
	return result.data


# ── 14. log_asset_state_change ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("log_asset_state_change", mutating=True, limit=guard.WRITE_LIMIT)
def log_asset_state_change(
	user: str,
	asset_name=None,
	action=None,
	notes=None,
	photo_file_token=None,
	gps_lat=None,
	gps_lon=None,
) -> dict:
	"""Record a state-change action on an asset. Validates the transition."""
	guard.require_scope(user)
	asset_name = str(asset_name or "").strip()
	if not asset_name:
		frappe.throw("asset_name is required.", frappe.ValidationError)
	action_str = str(action or "").strip()
	if not action_str:
		frappe.throw("action is required.", frappe.ValidationError)

	result = asset_tags.log_asset_state_change(
		{
			"asset_name": asset_name,
			"action": action_str,
			"performed_by": user,
			"notes": str(notes or "").strip() or None,
			"photo_file_token": str(photo_file_token or "").strip() or None,
			"gps_lat": gps_lat,
			"gps_lon": gps_lon,
		}
	)
	return result.data


# ── 15. get_available_actions ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_available_actions", limit=guard.READ_LIMIT)
def get_available_actions(user: str, asset_name=None) -> dict:
	"""What state-change actions can be performed on this asset right now."""
	guard.require_scope(user)
	asset_name = str(asset_name or "").strip()
	if not asset_name:
		frappe.throw("asset_name is required.", frappe.ValidationError)

	result = asset_tags.get_available_actions({"asset_name": asset_name})
	return result.data


# ── 16. report_asset_issue ──────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("report_asset_issue", mutating=True, limit=guard.WRITE_LIMIT)
def report_asset_issue(
	user: str,
	asset_name=None,
	description=None,
	urgency=None,
	photo_file_token=None,
	task_type=None,
	skill_required=None,
	gps_lat=None,
	gps_lon=None,
) -> dict:
	"""Report a problem on a specific asset. Convenience wrapper that auto-fills
	location and skill from the asset, then creates a Farm Task."""
	allowed = guard.require_scope(user)
	employee = _employee(user)

	asset_name = str(asset_name or "").strip()
	if not asset_name:
		frappe.throw("asset_name is required.", frappe.ValidationError)

	inner = {
		"asset_name": asset_name,
		"reported_by": employee,
	}
	if description:
		inner["description"] = str(description).strip()
	if urgency:
		inner["urgency"] = str(urgency).strip()
	if photo_file_token:
		inner["photo_file_token"] = str(photo_file_token).strip()
	if task_type:
		inner["task_type"] = str(task_type).strip()
	if skill_required:
		inner["skill_required"] = str(skill_required).strip()
	if gps_lat is not None:
		inner["gps_lat"] = gps_lat
	if gps_lon is not None:
		inner["gps_lon"] = gps_lon

	company = guard.require_company(user, None, allowed) if allowed else ""
	if not company and allowed:
		inner["company"] = allowed[0]
	elif company:
		inner["company"] = company

	result = asset_tags.report_asset_issue(inner)
	data = result.data
	return shape.task(data, None)


# ── 17. create_employee ─────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_employee", mutating=True, limit=guard.WRITE_LIMIT)
def create_employee(
	user: str,
	first_name=None,
	middle_name=None,
	last_name=None,
	employee_name=None,
	company=None,
	gender=None,
	date_of_birth=None,
	date_of_joining=None,
	employment_type=None,
	designation=None,
	department=None,
	branch=None,
	personal_email=None,
	cell_number=None,
	i9_status=None,
	w4_status=None,
	jurisdiction=None,
) -> dict:
	"""The person. Step 1 of the wizard, and the record the other four steps fill in.

	v0.46.0, and the same failure v0.45.0 fixed nine of: `OnboardingAPI` reached
	for `POST /api/resource/Employee`, the Tailscale funnel publishes
	`/farmops/api/…` and nothing else, and the wizard 404'd on its FIRST step —
	so none of the nine paths that release published was ever reached from a
	phone. `MobileAPI.createEmployee` has named this path since Sprint 9.

	IT DELEGATES RATHER THAN INSERTING. `frappe.get_doc({...}).insert()` here would
	be four lines and would step around every rule `tools/employee.py` has held
	since v0.18.1: the twenty-two-field allowlist that refuses `ctc` and
	`salary_structure` by name, the second-record check that keeps one person off
	the dispatch board twice, the mandatory fields read off THIS site's meta rather
	than assumed, and `require_hr_role`. Those rules stay where they are for the
	reason the dispatch rules do — a wrapper with its own copy is a second set of
	personnel rules to keep in step, and they drift.

	`status` IS NOT ACCEPTED, and the app sends it. `OnboardingIdentity.employeePayload`
	carries `"status": "Active"`, which is what `create_employee` writes anyway;
	what the argument would ALSO buy is a phone that can file somebody as Left or
	Suspended on the day they were hired. It is dropped rather than forwarded, and
	the record comes out Active because that is the tool's default.

	`user_id` IS NOT ACCEPTED EITHER. Linking an Employee to a login is what turns
	a login into a person on the dispatch board, and `link_employee_to_user` does
	it in the Desk behind a check that the account is actually enrolled. A phone
	that could set it in passing could point somebody else's task history at an
	account it names.

	THE THREE COMPLIANCE STATUSES ARE FORWARDED, NOT DEFAULTED HERE. v0.46.1: the
	wizard reached this path and got "this site's Frappe HR marks i9_status,
	w4_status, jurisdiction mandatory on Employee, and the call did not supply
	them" — which was not Frappe HR's doing at all. `compliance_fields.py` installs
	those three as Custom Fields with `reqd=True`, so the wall was erpnext_mcp's
	own, and it stood in front of `onboard_employee` and the MCP tool exactly as
	much as it stood in front of the phone.

	The obvious fix was three lines HERE — Pending, Missing, OR — and it would have
	been the wrong file. `tools/employee.py` owns the fourteen-field allowlist and
	the mandatory check, so a wrapper cannot pass a field the allowlist does not
	carry, and a wrapper that could would be a second set of hiring defaults to
	keep in step with `onboard_employee`'s. The defaults live in the tool, next to
	the check they answer; all three are on `WRITABLE` now, so what this wrapper
	adds is the ABILITY TO OVERRIDE. The wizard sends none of them today and gets
	the tool's values back in `defaults_applied`; a later build that asks the
	foreman which state the crew is working can send `jurisdiction` and have it
	honoured without a server change.
	"""
	allowed = guard.require_scope(user)

	inner = {
		"employee_name": _full_name(first_name, last_name, employee_name),
		"company": _company(user, company, allowed),
	}
	for key, value in (
		("first_name", first_name),
		# v0.51.0. Read off the licence barcode at the tailgate and dropped
		# here until now — which also emptied the I-9's Legal Middle Name, since
		# `submit_i9_section_1` fills that from `Employee.middle_name` when the
		# caller sends none.
		("middle_name", middle_name),
		("last_name", last_name),
		("gender", gender),
		("date_of_birth", date_of_birth),
		("date_of_joining", date_of_joining),
		("employment_type", employment_type),
		("designation", designation),
		("department", department),
		# v0.54.0. The Assignment step's fourth dropdown, and the one that had
		# nowhere to land: `tools/employee.WRITABLE` did not carry `branch`, so a
		# wizard that asked which camp somebody was hired to could not record the
		# answer. `list_onboarding_reference_data` is where the four choices come
		# from, and `create_employee`'s Link check refuses one that names nothing.
		("branch", branch),
		("personal_email", personal_email),
		("cell_number", cell_number),
		("i9_status", i9_status),
		("w4_status", w4_status),
		("jurisdiction", jurisdiction),
	):
		if value not in (None, ""):
			inner[key] = value

	result = personnel.create_employee(inner)
	return _employee_identity(result.data["employee"])


# ── 18. search_employees ────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("search_employees", limit=guard.READ_LIMIT)
def search_employees(user: str, query=None, company=None) -> dict:
	"""Who this entity has already hired, by name. The step before creating a second record.

	The wizard runs this before its Identity step writes anything: a picker who
	worked last season is an Employee record that already exists, and hiring them
	again as a NEW record is the mistake `create_employee` refuses at the end and
	this method prevents at the beginning. A match the foreman recognises becomes
	`reactivate_employee`; no match becomes `create_employee`.

	IT CARRIES THE HR ROLE GATE ITSELF, which no other read on this surface does.
	The rest of the file reads field work — a task board, an asset, a compliance
	alert — and a picker holding a phone is entitled to all of it. This reads the
	personnel register: every name, hire date and employment type an entity has,
	including the people who have LEFT. `tools/hr.list_employees` has no gate of
	its own because the MCP transport is an operator's console; this transport
	faces the open internet with a phone on the other end, so the same gate the
	writing methods inherit from `tools/employee.py` is applied here by hand.

	STATUS IS NOT FILTERED, on purpose. A Left or Inactive employee is exactly who
	this search is for, and `hr.list_employees` defaults to Active — which is why
	this reads the register directly rather than calling it. `ExistingEmployee`
	carries `status` and the wizard branches on it: Active means "you already have
	them", anything else means "reactivate".

	The company filter is the caller's own entities when the app does not name one,
	never the whole site — the rule `list_available_tasks` states at length — and
	`guard.scoped` checks the answer again on the way out.

	v0.106.0 ADDS `designation`, `mobile_roles`, `primary_role` AND `can_dispatch`,
	AND THE REASON IS A PICKER THAT WAS FILTERING ON NOTHING. This is the roster
	half of every "who should hold this" screen in the app, and until now the only
	role the mobile surface reported was the CALLER's, from
	`get_current_user_context`. So a sheet asking "who may approve this" offered
	the whole crew, the foreman picked a picker, and the refusal arrived after the
	choice — a 403 about somebody else's roles, which reads as the feature being
	broken.

	`designation` AND `mobile_roles` ARE DIFFERENT REGISTERS AND BOTH ARE HERE.
	A designation is what somebody does all day and carries no permission; a role
	is what they may touch. A Checker has a designation and no role at all. See
	the job-title table in `roles.py`, which exists because this distinction keeps
	being asked about in the wrong shape.

	`can_dispatch` IS A COURTESY AND NOT THE BOUNDARY. `guard.require_dispatch_role`
	still runs on every dispatching call and is unchanged; this exists so a picker
	can grey a row out rather than let somebody discover the refusal after they
	have chosen. It is computed from the same frozenset the gate refuses on, so
	the two cannot come to disagree.

	`user_id` IS READ AND NOT RETURNED. The roles hang off the login, so it has to
	be fetched; handing it back would publish the login of every person on the
	register, which is a credential identifier and not a roster fact.
	"""
	allowed = guard.require_scope(user)
	personnel.require_hr_role()
	wanted = guard.require_company(user, company, allowed)

	text = str(query or "").strip()
	if not text:
		frappe.throw(
			"query is required — a search with nothing in it would return the entity's whole "
			"personnel register.",
			frappe.ValidationError,
		)

	rows = frappe.db.get_all(
		EMPLOYEE,
		filters={
			"company": ("in", [wanted] if wanted else allowed),
			"employee_name": ("like", f"%{text}%"),
		},
		fields=compat.existing_fields(EMPLOYEE, list(EMPLOYEE_SEARCH_FIELDS)),
		order_by="employee_name asc",
		limit_page_length=EMPLOYEE_SEARCH_LIMIT,
	)

	found = guard.scoped(
		[
			{
				"name": row.get("name"),
				"employee_name": row.get("employee_name"),
				"employee_id": str(row.get("employee_number") or "") or None,
				"status": row.get("status"),
				"date_of_joining": row.get("date_of_joining"),
				"employment_type": row.get("employment_type"),
				"company": row.get("company"),
				"designation": row.get("designation") or None,
				# v0.106.0. WHAT EACH PERSON MAY DO, so a picker can stop offering
				# work to somebody the server is about to refuse. See
				# `roles.capability_of` — `can_dispatch` is computed off the same
				# frozenset `guard.require_dispatch_role` refuses on, and it is a
				# COURTESY rather than the boundary: the gate is still on every call.
				**_capability(row.get("user_id")),
			}
			for row in rows or []
		],
		allowed,
	)
	return {
		"employees": found,
		"count": len(found),
		"company": wanted or None,
		"limit": EMPLOYEE_SEARCH_LIMIT,
	}


# ── 19. get_employee ────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_employee", limit=guard.READ_LIMIT)
def get_employee(user: str, employee=None, docname=None) -> dict:
	"""One person's record and how far through onboarding they already are.

	v0.46.2, and the third of `OnboardingAPI`'s Identity-step calls to be reaching
	somewhere the funnel does not publish: `getEmployeeDetail` still asks
	`GET /api/resource/Employee/<name>` (`OnboardingAPI.swift:121`), which is the
	same 404 v0.46.0 fixed for the other two. It is the step BETWEEN them — the
	foreman picks a returning picker out of `search_employees`, and the wizard
	then has to decide which of its five steps that person still needs, because a
	worker who came back for their fourth season should not be walked through an
	I-9 and a W-4 they already have on file. In tree fruit that is the COMMON
	path, not the exception.

	WHAT IT ANSWERS WITH is `EmployeeDetail` (`OnboardingModels.swift:291`) field
	for field — `name` and `employee_name` strict, the rest optional — plus
	`jurisdiction`, which the struct does not carry yet and which the W-4 step
	needs the moment a crew works across a state line. `needsI9`, `needsW4` and
	`needsBadge` are computed on the handset from those fields and are NOT
	computed here: a server that also decided which step to skip would be a second
	copy of the wizard's own rule, and this file refuses that for the dispatch
	rules at length.

	`badge_id` IS A LOOKUP, NOT A COLUMN. `link_badge_to_employee` writes a Bucket
	Log Badge Map row rather than a field on Employee, and only an ACTIVE mapping
	counts — a badge handed back at the end of last season is exactly the one step
	5 has to issue again.

	`i9_status` AND `w4_status` ARE RECONCILED BEFORE THEY GO OUT, and that is the
	whole reason this method is worth writing rather than pointing the app at a
	column. Those two are Custom Fields this app installs; `create_employee` sets
	them to Pending/Missing and NOTHING MOVES THEM AFTERWARDS — `submit_i9_section_2`
	and `submit_w4` write `I-9 Form.status` and `W-4 Form.status` on their own
	doctypes. `EmployeeDetail.satisfiedSteps` (`OnboardingModels.swift:352`)
	branches on the COLUMN, so handing it over raw would take a returning picker
	whose I-9 was verified last June through a fresh I-9 and a fresh W-4. A live
	Complete/Active record therefore fills a column that is still at its hire-time
	default, and NOTHING ELSE: `Expired` and `Requires-Update` stand, because an
	expired I-9 is precisely the case that must be re-verified. `i9_status_recorded`,
	`w4_status_recorded`, `i9`, `w4` and `reconciled` carry the unreconciled truth
	beside it — see `tools/employee.employee_detail`, which owns the rule.

	IT IS THE ONE READ ON THIS SURFACE WHOSE GATE HAS AN EXCEPTION IN IT.
	`search_employees` applies `require_hr_role` flatly, and rightly: it hands back
	the entity's whole personnel register, which is not a picker's to browse. This
	names ONE record, and a worker asking for their own — their hire date, their
	I-9 status, the badge in their pocket — is not reading the register at all. So
	the HR role is required for ANYBODY ELSE'S record and not for the caller's own,
	which is the narrowest opening that makes the sentence "workers can check their
	own onboarding" true. `_employee` resolves the caller through `Employee.user_id`
	and nothing in the body, so the exception cannot be claimed by naming somebody.

	`docname` IS ACCEPTED AS A SECOND SPELLING of `employee`, for the reason
	`reactivate_employee` accepts it: the Swift function's own parameter is called
	`docname`, and two names for one docname is cheaper than shipping a build.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee or docname, allowed)

	if person != fieldwork._employee_for(user):
		personnel.require_hr_role()

	detail = personnel.employee_detail(person)
	if not guard.scoped([detail], allowed):  # pragma: no cover - require_scoped_doc got there first
		frappe.throw(f"employee {person} was not found.", frappe.DoesNotExistError)
	return detail


# ── 20. reactivate_employee ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("reactivate_employee", mutating=True, limit=guard.WRITE_LIMIT)
def reactivate_employee(user: str, employee=None, docname=None) -> dict:
	"""Put a returning picker back on the payroll: Active, joined today.

	The other half of `search_employees`. A worker who left in November and is
	standing in the yard in June is one Employee record with a status, not two
	records with one history between them — and the wizard's own flow takes this
	branch before it will consider creating anything.

	IT WRITES `date_of_joining` AND THAT OVERWRITES THE ORIGINAL HIRE DATE. This is
	what `OnboardingAPI.reactivateEmployee` already did through the REST path it
	could not reach, and it is deliberate rather than incidental: the I-9 opened a
	few screens later is checked against the hire date, and 8 U.S.C. §1324a's
	three-business-day clock counts from the day this person started THIS time. The
	date it replaced is not lost — `update_employee` reports every field it changed
	with its before-value, and that lands in the MCP Action Log row this call
	writes.

	TODAY IS NOT AN ARGUMENT. A rehire date is a wage fact — it decides which
	season's tenure a person is credited with — and the phone in the yard knows
	exactly one true answer to it, which is the day it is being held. A backdated
	rehire is a correction, and corrections are made in the Desk by somebody who
	can see what they are correcting.

	`docname` IS ACCEPTED AS A SECOND SPELLING of `employee`, because the Swift
	function's own parameter is called `docname` and the two names for one docname
	is the cheapest possible way to not ship a build over a key. Both are checked
	the same way — `_employee_argument` proves the record is inside this caller's
	entities, so an Employee of an entity this account cannot see reads as not
	found rather than as refused.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee or docname, allowed)

	# v0.94.0. `personnel.reactivate_employee`, NOT `personnel.update_employee`.
	# The two fields written here have not changed and neither has anything this
	# call does — what changed is which gate it passes. Rehiring is step 1b of a
	# hire, so it takes `require_hiring_role` with the rest of the hiring flow,
	# while the general register edit stays on `require_hr_role`. Routing through
	# the narrow tool is what keeps those two facts from being the same fact: it
	# takes no field arguments at all, so a foreman reaching this endpoint cannot
	# reach a department, a supervisor or a login through it.
	result = personnel.reactivate_employee({"employee": person})
	return {**_employee_identity(person), "changed": result.data.get("changed") or []}


# ── 21. create_i9_form ──────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_i9_form", mutating=True, limit=guard.WRITE_LIMIT)
def create_i9_form(user: str, employee=None, company=None, hire_date=None) -> dict:
	"""Open a Draft I-9 for somebody who has just been hired.

	The first of the five onboarding methods, and the only one that creates the
	record the other four fill in. `OnboardingAPI.createI9Form` sends today as the
	hire date because the app's flow runs on the person's first morning; it is NOT
	defaulted here when absent, because the three-business-day clock Section 2 is
	checked against counts from it and a guessed hire date would silently move a
	statutory deadline.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee, allowed)
	result = i9.create_i9_form(
		{
			"employee": person,
			"company": _company(user, company, allowed),
			"hire_date": hire_date,
		}
	)
	return result.data


# ── 22. submit_i9_section_1 ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("submit_i9_section_1", mutating=True, limit=guard.WRITE_LIMIT)
def submit_i9_section_1(
	user: str,
	employee=None,
	citizenship_status=None,
	ssn_last_four=None,
	ssn=None,
	gps_lat=None,
	gps_lon=None,
	address_street=None,
	address_city=None,
	address_state=None,
	address_zip=None,
	alien_registration_number=None,
	i94_admission_number=None,
	foreign_passport_number=None,
	foreign_passport_country=None,
	work_authorization_expiry=None,
	legal_first_name=None,
	legal_last_name=None,
	legal_middle_name=None,
	date_of_birth=None,
	email=None,
	phone=None,
	section_1_signature=None,
) -> dict:
	"""The employee's own half of the I-9: who they are and how they may work.

	THE LEGAL NAMES FALL BACK TO THE EMPLOYEE RECORD, which is what makes this
	callable from the shipped app at all. `submit_i9_section_1` requires
	`legal_first_name` and `legal_last_name`; `OnboardingI9Section1.apiParams`
	sends neither, because step 1 of the app's own flow already created the
	Employee with them and asking a person to type their name twice on a phone in
	a packing shed is how a form gets abandoned. Sent explicitly they win — a
	legal name and a payroll name genuinely differ for some people, and the I-9
	wants the legal one.

	`work_authorization_expiry` IS RENAMED, not forwarded. The doctype's column is
	`alien_work_authorization_expiry` and the app's key is the shorter one; a
	rename here is one line, and the alternative is a build shipped to every phone
	in the valley. The tool reads it only for "Alien Authorized to Work", so a
	value sent alongside any other citizenship status is dropped there rather than
	here — that is the tool's rule about its own field.

	THE OTHER TWO ALIEN IDENTIFIERS ARE FORWARDED UNRENAMED. v0.47.0 taught the
	tool that Section 1 takes an A-Number, an I-94 admission number, OR a foreign
	passport with the country that issued it — any one of the three answers the
	question — and this wrapper carried only the first, so a worker holding an
	I-94 and no A-Number could fill the form on a phone and be refused for a field
	the transport had dropped. `i94_admission_number`, `foreign_passport_number`
	and `foreign_passport_country` go through under the tool's own names because
	the doctype's columns and USCIS's wording agree, so there is nothing to
	rename. That a passport number without its country is refused, and that all
	three are read only for "Alien Authorized to Work", are the tool's rules.

	`preparer_used` and the three preparer fields ARE NOT ACCEPTED. A preparer or
	translator signs their own attestation on paper, and a phone that could set
	`preparer_used` without carrying that signature would record an attestation
	nobody made. An operator files those in the Desk.

	`ssn` TAKES ALL NINE DIGITS AND IS NEW IN v0.136.0. `ssn_last_four` is
	unchanged and remains what a handset should send by default. The tool has
	accepted a full `ssn` since v0.47.0 and this transport did not carry one, so
	a site that had switched `store_full_ssn` ON in I-9 Settings — the deliberate
	act of saying "we run E-Verify and we keep the whole number" — still could
	not be given it by the app that collects it, and the encrypted column stayed
	empty on every phone-filed form. The wizard holds all nine (it draws two
	federal forms from them) and threw eight away at the transport.

	BOTH GATES STILL APPLY AND NEITHER IS THIS TRANSPORT'S. `tools/i9` reduces
	whatever arrives to four digits for `ssn_last_four` regardless, and writes
	the nine to the encrypted `ssn_full` column ONLY where `store_full_ssn` is
	on. A phone that sends nine to a site that never asked for them has written
	four, exactly as before. `get_i9_form` now reports that switch back to the
	client so the app can send four to a site that wants four — the reason it
	sent four unconditionally was that it had no way to find out.

	`gps_lat` AND `gps_lon` ARE RECORDED ONLY ALONGSIDE A SIGNATURE, which is the
	tool's rule and not this one's: they land in `section_1_signed_gps` in the
	same branch that stamps the moment and the address, so a section nobody
	signed does not acquire a location. Corroboration rather than verification —
	the server cannot check where a handset says it was.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee, allowed)
	row = (
		frappe.db.get_value(EMPLOYEE, person, ["first_name", "middle_name", "last_name"], as_dict=True) or {}
	)

	inner = {
		"employee": person,
		"citizenship_status": citizenship_status,
		"legal_first_name": legal_first_name or row.get("first_name"),
		"legal_last_name": legal_last_name or row.get("last_name"),
		"legal_middle_name": legal_middle_name or row.get("middle_name"),
		"alien_work_authorization_expiry": work_authorization_expiry,
	}
	for key, value in (
		("ssn_last_four", ssn_last_four),
		("ssn", ssn),
		("gps_lat", gps_lat),
		("gps_lon", gps_lon),
		("address_street", address_street),
		("address_city", address_city),
		("address_state", address_state),
		("address_zip", address_zip),
		("alien_registration_number", alien_registration_number),
		("i94_admission_number", i94_admission_number),
		("foreign_passport_number", foreign_passport_number),
		("foreign_passport_country", foreign_passport_country),
		("date_of_birth", date_of_birth),
		("email", email),
		("phone", phone),
		("section_1_signature", section_1_signature),
	):
		if value is not None:
			inner[key] = value

	result = i9.submit_i9_section_1(inner)
	return result.data


# ── 23. submit_i9_section_2 ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("submit_i9_section_2", mutating=True, limit=guard.WRITE_LIMIT)
def submit_i9_section_2(
	user: str,
	employee=None,
	document_path=None,
	verifier_name=None,
	verifier_title=None,
	verification_date=None,
	list_a_doc_type=None,
	list_a_doc_number=None,
	list_a_authority=None,
	list_a_expiry=None,
	list_a_is_receipt=None,
	list_b_doc_type=None,
	list_b_doc_number=None,
	list_b_authority=None,
	list_b_expiry=None,
	list_b_is_receipt=None,
	list_c_doc_type=None,
	list_c_doc_number=None,
	list_c_authority=None,
	list_c_expiry=None,
	list_c_is_receipt=None,
	document_copies_stored=None,
	section_2_signature=None,
	gps_lat=None,
	gps_lon=None,
) -> dict:
	"""The employer's half: what documents were examined, by whom, on what day.

	THREE KEYS PER DOCUMENT ARE RENAMED. `OnboardingI9Section2.apiParams` sends
	`list_a_doc_type`, `list_a_authority` and `list_a_expiry`; the doctype's
	columns are `list_a_doc_title`, `list_a_doc_authority` and `list_a_doc_expiry`.
	Same for B and C. Renaming here rather than in Swift is the same trade
	`api/shape.py` makes and states: the backend moves, because the alternative is
	a new build on every phone.

	BOTH DOCUMENT PATHS ARE FORWARDED WHOLE and the tool picks. It requires
	`list_a_doc_title` on the List A path and both `list_b_doc_title` and
	`list_c_doc_title` on the other, refuses a `document_path` that is neither,
	and refuses a verification more than three business days after the hire date —
	all of which is 8 U.S.C. §1324a's rule rather than this transport's, so none
	of it is restated here.

	THE THREE RECEIPT FLAGS ARE NOT RENAMED and they are the reason this wrapper
	changed. v0.47.0 taught the tool 8 CFR 274a.2(b)(1)(vi) — a worker whose
	document was lost, stolen or damaged presents a receipt for the replacement
	and may lawfully work while it comes — and the transport dropped the flag, so
	every receipt examined on a phone was filed as though the document itself had
	been seen. That is a false attestation on a federal form, and the 90-day clock
	`receipt_expires_on` starts never started. `list_a_is_receipt`,
	`list_b_is_receipt` and `list_c_is_receipt` are booleans; unsent, the tool
	defaults each to false, which is the pre-v0.47.0 behaviour for a caller that
	has not grown the checkbox yet.

	A RECEIPT STILL COMPLETES THE FORM AND STILL NEEDS ITS TITLE. Neither is this
	transport's rule: the tool sets Complete because the person may work, and
	checks the title because a receipt is a receipt FOR a named document.

	`verifier_name` IS THE TYPED ONE, not the caller's. The person examining the
	documents signs their own name to the attestation, and the account that made
	the HTTP call is recorded regardless — every mobile call writes an MCP Action
	Log row naming it.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee, allowed)

	inner = {
		"employee": person,
		"document_path": document_path,
		"verifier_name": verifier_name,
		"verifier_title": verifier_title,
		"verification_date": verification_date,
	}
	for key, value in (
		("list_a_doc_title", list_a_doc_type),
		("list_a_doc_number", list_a_doc_number),
		("list_a_doc_authority", list_a_authority),
		("list_a_doc_expiry", list_a_expiry),
		("list_a_is_receipt", list_a_is_receipt),
		("list_b_doc_title", list_b_doc_type),
		("list_b_doc_number", list_b_doc_number),
		("list_b_doc_authority", list_b_authority),
		("list_b_doc_expiry", list_b_expiry),
		("list_b_is_receipt", list_b_is_receipt),
		("list_c_doc_title", list_c_doc_type),
		("list_c_doc_number", list_c_doc_number),
		("list_c_doc_authority", list_c_authority),
		("list_c_doc_expiry", list_c_expiry),
		("list_c_is_receipt", list_c_is_receipt),
		("document_copies_stored", document_copies_stored),
		("section_2_signature", section_2_signature),
		# v0.136.0. Where the verifier stood when they certified that they had
		# examined the documents in their hand. Recorded only alongside the
		# signature, by the tool, for the reason Section 1's is.
		("gps_lat", gps_lat),
		("gps_lon", gps_lon),
	):
		if value is not None:
			inner[key] = value

	result = i9.submit_i9_section_2(inner)
	return result.data


# ── 24. list_i9_document_types ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_i9_document_types", limit=guard.READ_LIMIT)
def list_i9_document_types(user: str, list_category=None) -> dict:
	"""What USCIS accepts, grouped the way Section 2 asks the question.

	THE APP HAD THIS LIST HARDCODED AND THE SERVER HAS HAD THE REAL ONE SINCE
	v0.27.0. `i9_documents.py` seeds all 24 accepted documents as records, an
	operator may deactivate any of them for their own site, and none of that could
	reach a phone — so the picker a foreman scrolls in the orchard was a Swift
	array that goes stale the next time USCIS revises the list, and goes stale
	silently. This is the read that makes it a lookup, and it is the FIRST read on
	the onboarding half of this surface: everything else here writes.

	GROUPED BY LIST, because that is the shape of the choice rather than a
	convenience. Section 2 is "one from List A" OR "one from List B and one from
	List C", and a form that has to make that split itself is a form with its own
	copy of which document is in which category — which is exactly the copy that
	was wrong. `documents` carries the flat list beside it for a caller that
	wants to search across all three.

	VIEW-ONLY AND NOT SCOPED TO AN ENTITY. The federal list of acceptable
	documents is not a fact about a company, and there is nothing on one of these
	rows that belongs to a person: a title, its USCIS code, whether it carries a
	photograph. `guard.endpoint` still runs the kill switch, the role gate, the
	enrolment gate and the rate limit, which is the whole of what this read needs.
	"""
	guard.require_scope(user)
	inner = {}
	category = str(list_category or "").strip().upper()
	if category:
		if category not in ("A", "B", "C"):
			frappe.throw(
				f"list_category {list_category!r} is not an I-9 list. Pass A, B, C, or "
				"nothing at all for the whole table.",
				frappe.ValidationError,
			)
		inner["list_category"] = category

	result = i9.list_i9_document_types(inner)
	grouped = result.data.get("by_list") or {}
	return {
		"documents": result.data.get("documents") or [],
		"count": result.data.get("count") or 0,
		"list_a": grouped.get("A") or [],
		"list_b": grouped.get("B") or [],
		"list_c": grouped.get("C") or [],
		"list_category": category or None,
	}


# ── 25. reverify_i9 ─────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("reverify_i9", mutating=True, limit=guard.WRITE_LIMIT)
def reverify_i9(
	user: str,
	employee=None,
	reason=None,
	document_title=None,
	document_number=None,
	issuing_authority=None,
	document_expiry=None,
	reverification_date=None,
	rehire_date=None,
	verifier_name=None,
	verifier_title=None,
	section_3_signature=None,
	notes=None,
) -> dict:
	"""Section 3, for the returning worker whose authorization ran out.

	THIS IS THE BRANCH THE WIZARD COULD SEE AND COULD NOT TAKE. v0.46.2 gave
	`get_employee` the reconciliation that reports a returning picker's I-9 as
	`Expired` rather than as done — deliberately, because an expired I-9 is
	precisely the case §1324a wants re-examined. What the handset could then do
	about it was nothing: `create_i9_form` refuses a second form for somebody who
	has one, and the only other door was a Desk edit over the columns recording
	what was examined on the day of hire. So the wizard's own answer to its
	hardest case was "find an operator with a laptop".

	`document_expiry` IS RENAMED ON NEITHER SIDE and the argument names are the
	tool's, because there is no shipped Swift struct for this call to be
	compatible with — this endpoint precedes the screen rather than following it.
	Where the app grows one, the renaming trade `submit_i9_section_2` makes is the
	one to make again: the backend moves.

	`verifier_name` IS THE TYPED ONE, not the caller's, for the reason Section 2
	states — the person who examined the document signs their own name, and the
	account that made the call is on the MCP Action Log row regardless.

	EVERY RULE IS THE TOOL'S. That reverification needs a signed Section 2 to
	follow, that List B is not a reverification document, that a document already
	expired on the day it was examined is not evidence of continuing
	authorization, that 'Rehire' needs a rehire date — all of it is 8 CFR
	274a.2(b)(1)(vii)'s and lives in `tools/i9.py`, so none of it is restated here.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee, allowed)

	inner = {
		"employee": person,
		"reason": reason,
		"document_title": document_title,
		"verifier_name": verifier_name,
	}
	for key, value in (
		("document_number", document_number),
		("issuing_authority", issuing_authority),
		("document_expiry", document_expiry),
		("reverification_date", reverification_date),
		("rehire_date", rehire_date),
		("verifier_title", verifier_title),
		("section_3_signature", section_3_signature),
		("notes", notes),
	):
		if value is not None:
			inner[key] = value

	result = i9.reverify_i9(inner)
	return result.data


# ── 26. submit_w4 ───────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("submit_w4", mutating=True, limit=guard.WRITE_LIMIT)
def submit_w4(
	user: str,
	employee=None,
	company=None,
	tax_year=None,
	filing_status=None,
	multiple_jobs=None,
	dependents_under_17=None,
	other_dependents=None,
	other_income=None,
	deductions=None,
	extra_withholding=None,
	additional_income_from_other_jobs=None,
) -> dict:
	"""Federal withholding, as the worker filled it in. Supersedes their last one.

	THREE MORE RENAMES, and they run the same way as Section 2's.
	`OnboardingW4.apiParams` sends `dependents_under_17`, `other_dependents` and
	`extra_withholding`; the doctype counts and periods, so its columns are
	`dependents_under_17_count`, `other_dependents_count` and
	`extra_withholding_per_period`.

	`status` IS NOT ACCEPTED AND NEITHER IS `effective_date`. `submit_w4` always
	writes an Active W-4 dated today and marks the previous one Superseded with a
	pointer to its replacement — that chain is what answers "which W-4 was in
	force on the day this cheque was cut", and a phone that could set either field
	could break it.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee, allowed)

	inner = {
		"employee": person,
		"company": _company(user, company, allowed),
		"tax_year": tax_year,
		"filing_status": filing_status,
	}
	for key, value in (
		("multiple_jobs", multiple_jobs),
		("dependents_under_17_count", dependents_under_17),
		("other_dependents_count", other_dependents),
		("other_income", other_income),
		("deductions", deductions),
		("extra_withholding_per_period", extra_withholding),
		("additional_income_from_other_jobs", additional_income_from_other_jobs),
	):
		if value is not None:
			inner[key] = value

	result = w4.submit_w4(inner)
	return result.data


# ── 27. link_badge_to_employee ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("link_badge_to_employee", mutating=True, limit=guard.WRITE_LIMIT)
def link_badge_to_employee(user: str, badge_id=None, employee=None, company=None, notes=None) -> dict:
	"""Point a scanned QR badge at the person holding it. The last onboarding step.

	`active` IS NOT ACCEPTED, which is the one thing this wrapper takes away.
	`link_badge_to_employee` uses it to DEACTIVATE a mapping, and a deactivated
	badge stops resolving on every bucket that scans it from that moment on —
	which is a decision about somebody's piece-rate pay, made in the Desk by
	somebody who can see the register. The wrapper always maps a badge live, which
	is the only thing the onboarding flow means by scanning one.

	Repointing a badge already mapped to somebody else IS allowed, because a lost
	card reissued to the next picker is the ordinary case rather than an attack —
	the tool records the previous holder on the row it returns, and the audit row
	names the account that did it.

	The backfill is the tool's and it matters here: a badge mapped after a morning
	of picking claims the buckets already synced against it that had nobody
	attached. A badge is scanned before the map exists more often than after.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee, allowed)
	badge = str(badge_id or "").strip()
	if not badge:
		frappe.throw("badge_id is required.", frappe.ValidationError)

	inner = {
		"badge_id": badge,
		"employee": person,
		"company": _company(user, company, allowed),
		"active": True,
	}
	if notes:
		inner["notes"] = str(notes).strip()

	result = bucket_log.link_badge_to_employee(inner)
	return result.data


# ── 27b. resolve_badge ──────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("resolve_badge", limit=guard.READ_LIMIT)
def resolve_badge(user: str, badge_id=None, company=None, shift=None) -> dict:
	"""Whose badge is this — the call between a scan and a name. v0.50.0.

	THE ONE READ THE SCANNING SIDE NEVER HAD. `add_worker_to_shift` takes an
	Employee docname and a camera produces a badge string, so the crew clock
	could scan a whole crew and had no way to turn any of it into the argument
	the roster call wants. The bucket loop had the same gap in a quieter form: it
	could show a foreman the code it read and never the picker's name, so a
	mis-scan looked exactly like a good one until the data reached a Desk.

	IT IS A PII LOOKUP KEYED ON A STRING ANYBODY HOLDING A CARD CAN PRODUCE, and
	that is why it is on `READ_LIMIT` rather than being cheap: sixty an hour is
	a crew clocking in and is not a register being enumerated. It answers only
	within the caller's own entities — `_company` is the same scope check every
	other method here runs — so a badge belonging to another entity on the site
	reads as "no such badge" rather than confirming it exists somewhere.

	IT REFUSES RATHER THAN ANSWERING EMPTY. Unknown, retired, and belonging to
	somebody who has left are three different sentences, because they are three
	different situations with three different fixes and the phone shows whichever
	one it got.

	`shift` IS OPTIONAL AND IS THE SECOND HALF OF THE QUESTION. Given one, the
	answer carries `on_shift` — whether this person is clocked in right now —
	which is what a bin trailer's scanner needs before it accepts a bucket.
	"""
	allowed = guard.require_scope(user)
	inner = {"badge_id": str(badge_id or "").strip(), "company": _company(user, company, allowed)}
	if shift:
		inner["shift"] = guard.require_scoped_doc(FARM_SHIFT, shift, "shift", allowed)
	result = badges.resolve_badge(inner)
	return result.data


# ── 27c. generate_employee_badge_qr ─────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("generate_employee_badge_qr", mutating=True, limit=guard.WRITE_LIMIT)
def generate_employee_badge_qr(
	user: str, employee=None, docname=None, company=None, regenerate=None, notes=None
) -> dict:
	"""Issue (or reprint) this hire's badge and hand back the QR to show them.

	THE TOOL HAS EXISTED SINCE v0.50.0 AND THE PHONE COULD NOT REACH IT. That is
	the whole of what this wrapper is. `generate_employee_badge_qr` mints a
	readable `CF-0001`, writes the register row a bucket scan resolves through
	and draws the code — and the only surface it was published on was the MCP
	tool registry, which the handset does not speak. So the wizard's badge step
	could map a card somebody had already printed elsewhere and could not
	produce one, on a hire day, in a yard, for a worker standing there waiting to
	be told their number.

	`badge_id` IS NOT ACCEPTED, and that is the one thing taken away here. The
	tool lets a caller name the identifier — a Desk operator adopting a card from
	the old `farm_app` uuid stock — and letting a handset do it would put the
	uniqueness of a payroll key in the hands of whatever a foreman typed. The
	phone's job is to ask for a badge; minting is the server's.

	`regenerate` IS ACCEPTED, because the lost-card path is a field problem and
	not a Desk one. Without it the call is IDEMPOTENT: somebody who already holds
	a live badge gets that badge's QR back rather than a second identifier, which
	is what makes the wizard's button safe to press twice on a bad connection.
	With it the old card is retired in the same call — a replacement that leaves
	its predecessor resolving is how a found badge keeps earning.

	THE HR ROLE AND THE ENTITY SCOPE ARE THE TOOL'S OWN. It calls
	`require_hr_role` and `require_company_scope` itself, and it refuses a
	worker who is not Active by name.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee or docname, allowed)

	inner = {"employee": person, "company": _company(user, company, allowed)}
	if regenerate is not None:
		inner["regenerate"] = regenerate
	if notes:
		inner["notes"] = str(notes).strip()

	result = badges.generate_employee_badge_qr(inner)
	data = result.data or {}
	return {
		"employee": data.get("employee"),
		"employee_name": data.get("employee_name"),
		"company": data.get("company"),
		"badge_id": data.get("badge_id"),
		"created": data.get("created"),
		"reused": data.get("reused"),
		"retired_badges": data.get("retired_badges") or [],
		"designation": data.get("designation"),
		# What a card needs to be drawn on the handset: the code, the face (or
		# the initials that stand in for one) and the entity's mark.
		"png_base64": data.get("png_base64"),
		"png_bytes": data.get("png_bytes"),
		"photo_url": data.get("photo_url"),
		"photo_placeholder": data.get("photo_placeholder"),
		"company_logo_url": data.get("company_logo_url"),
	}


# ── 27d. get_employee_badge_pass ────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("get_employee_badge_pass", mutating=True, limit=guard.WRITE_LIMIT)
def get_employee_badge_pass(
	user: str, employee=None, docname=None, company=None, platform=None, regenerate=None
) -> dict:
	"""The badge as a file the foreman AirDrops into the worker's own wallet.

	THE DELIVERY THIS SURFACE WAS MISSING. `generate_employee_badge_qr` hands
	back a PNG, and a PNG has to be printed, laminated and carried — which is a
	trip back to an office in the middle of a hire day, and a card that goes
	through a wash cycle in August. Every worker in the orchard already has a
	phone with a wallet on it. This returns a `.pkpass` the foreman shares
	straight off the handset: it opens into Apple Wallet on the worker's device
	with nothing installed there, and the Android half is a save link in the same
	answer.

	THE BYTES ARE IN THE RESULT AND THAT IS WHY THIS ROUTE EXISTS. The tool
	attaches the pass to the Employee as a private File and hands back a
	`file_url`, which is right for a Desk operator and useless to a handset — the
	app authenticates against THIS door with `X-FarmOps-Token`, not with a Frappe
	session, so a private file URL is a login page to it. `include_base64` is
	therefore set here and is not a body key: a phone that could turn it off
	would be a phone that cannot share what it just asked for.

	`badge_id` IS NOT ACCEPTED, for the reason it is not accepted on
	`generate_employee_badge_qr`: minting a payroll key is the server's job, not
	whatever a foreman typed. `regenerate` IS, because the lost-card path happens
	in a field — and without it the call is IDEMPOTENT, so the wizard's button is
	safe to press twice on a bad connection.

	`attach` IS NOT ACCEPTED EITHER. The pass is always filed against the
	Employee, so a reissue has a record and a Desk operator can hand the same
	file to somebody who lost their phone rather than reissuing a badge for it.

	AN UNSIGNED PASS COMES BACK AS UNSIGNED. On a site with no Apple certificate
	the file is complete and correct and `apple.signed` is false with the reason
	in it — the app should say so rather than sharing a file Wallet will refuse.
	Every other refusal is the tool's: an inactive worker, an entity this account
	cannot reach, an employee of another company.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee or docname, allowed)

	inner = {
		"employee": person,
		"company": _company(user, company, allowed),
		# See the docstring: the handset cannot fetch a private File, so the
		# bytes travel in the answer. Not a body key.
		"include_base64": True,
	}
	if platform:
		inner["platform"] = platform
	if regenerate is not None:
		inner["regenerate"] = regenerate

	result = wallet_tools.generate_employee_badge_pass(inner)
	data = result.data or {}
	apple = data.get("apple") or {}
	google = data.get("google") or {}
	return {
		"employee": data.get("employee"),
		"employee_name": data.get("employee_name"),
		"company": data.get("company"),
		"badge_id": data.get("badge_id"),
		"created": data.get("created"),
		"reused": data.get("reused"),
		"retired_badges": data.get("retired_badges") or [],
		"platform": data.get("platform"),
		"warnings": data.get("warnings") or [],
		# The Apple half, flattened to what a share sheet needs: the bytes, what
		# to call the file, and the UTI that makes iOS open it in Wallet rather
		# than in Files. `pass_json` is deliberately NOT forwarded — it is a
		# debugging read for the Desk and the app has no use for a second copy of
		# what is already inside the archive it was handed.
		"apple": {
			"pkpass_base64": apple.get("pkpass_base64"),
			"file_name": apple.get("file_name"),
			"content_type": apple.get("content_type"),
			"bytes": apple.get("bytes"),
			"sha256": apple.get("sha256"),
			"signed": apple.get("signed"),
			"configured": apple.get("configured"),
			"warnings": apple.get("warnings") or [],
		}
		if data.get("apple")
		else None,
		"google": {
			"save_url": google.get("save_url"),
			"signed": google.get("signed"),
			"configured": google.get("configured"),
			"warnings": google.get("warnings") or [],
		}
		if data.get("google")
		else None,
	}


# ── 27e. set_employee_photo ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("set_employee_photo", mutating=True, limit=guard.WRITE_LIMIT)
def set_employee_photo(user: str, employee=None, docname=None, file_token=None) -> dict:
	"""File a headshot against the Employee and make it the photo on the record.

	THE BADGE CARD READS `Employee.image` AND NOTHING WAS EVER WRITING IT.
	`attach_onboarding_document` files evidence — the bytes land as a private
	File pointing at the Employee and the Employee points nowhere — which is
	right for a List B photograph and leaves a badge printing initials. This is
	the same staged upload followed by the field update that closes the loop.

	IT TAKES A `file_token`, NOT BYTES, exactly like every other upload on this
	surface: `stage_file_chunk` then `finalize_staged_file`, and this call names
	what that produced. One upload path, and it is the one that authenticates.

	THE HR ROLE IS REQUIRED WITH NO EXCEPTION, the same posture
	`attach_onboarding_document` takes and for the same reason: an account that
	could set its own photograph is an account that could put somebody else's
	face on its own badge.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee or docname, allowed)
	personnel.require_hr_role()

	if not str(file_token or "").strip():
		frappe.throw(
			"file_token is required — upload the photograph with stage_file_chunk and "
			"finalize_staged_file first, then send the token that returns.",
			frappe.ValidationError,
		)

	result = personnel.set_employee_photo({"employee": person, "file_token": file_token})
	data = result.data or {}
	return {
		"employee": data.get("employee"),
		"photo_url": data.get("photo_url"),
		"file_token": data.get("file_token"),
		"file_name": data.get("file_name"),
		"image_set": data.get("image_set"),
		"replaced": data.get("replaced"),
		"already_attached": data.get("already_attached"),
	}


# ── 28. sync_bucket_entries ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("sync_bucket_entries", mutating=True, limit=guard.UPLOAD_LIMIT)
def sync_bucket_entries(user: str, entries=None, company=None, shift=None) -> dict:
	"""A handset's capture queue, filed as Bucket Log Entry rows.

	`UPLOAD_LIMIT` RATHER THAN `WRITE_LIMIT`, and for the reason that limit exists.
	A picker works through a morning with no signal and the queue drains in a
	burst when the phone finds the yard's wifi; ten calls a minute would refuse
	most of it, and a refused sync is a morning of somebody's piece-rate sitting
	on a device that might not come back. The batch cap and the tool's own
	deduplication are what bound this instead — resending a batch the site already
	has creates nothing, so a client that retries because it never saw the answer
	is a no-op rather than a double payment.

	ONE COMPANY FOR THE WHOLE BATCH, checked once — see `_bucket_entries`, which
	also says why an entry may not name its own picker.

	THE BADGE POLICY IS `strict` HERE AND IS NOT A BODY KEY. v0.50.0. The tool
	defaults to `lenient` — file the capture, resolve the badge later — and that
	is right for a Desk import of a morning taken before anybody mapped the
	cards. It is wrong for a phone. Badges are minted by this app now
	(`generate_employee_badge_qr` writes the register at the moment the card is
	printed), so a handset scanning a string this site never issued has scanned a
	barcode on a soda can, a Wi-Fi join code or an operator's login QR — and
	filing that produces a piecework row nobody will ever claim, which is worse
	than the refusal the picker's foreman can act on while still standing there.
	A body key that could relax it would hand that decision to the handset.

	`shift` IS OPTIONAL AND IS THE OTHER HALF OF IT. Given the shift the crew
	clock has open, every capture is checked against its roster: a badge that
	resolves to somebody who is not clocked in is refused with their name in the
	sentence. Omitted, the badge still has to resolve to an employed person.
	"""
	allowed = guard.require_scope(user)
	wanted = _company(user, company, allowed)
	inner = {
		"entries": _bucket_entries(entries, wanted),
		"badge_policy": bucket_bridge.BADGE_POLICY_STRICT,
	}
	if shift:
		inner["shift"] = guard.require_scoped_doc(FARM_SHIFT, shift, "shift", allowed)
	result = bucket_log.sync_bucket_entries(inner)
	return result.data


# ── 29. start_shift ─────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("start_shift", mutating=True, limit=guard.WRITE_LIMIT)
def start_shift(
	user: str,
	company=None,
	location=None,
	farm_location_gps=None,
	shift_type=None,
	start_datetime=None,
	crew_employees=None,
	latitude=None,
	longitude=None,
) -> dict:
	"""Open a shift: a crew, a place, and the exposure period compliance is read against.

	`foreman` IS NOT ACCEPTED AND IS FILLED FROM THE CALLER. This is the strongest
	version of the rule `report_field_task` and `list_my_tasks` already follow, and
	here it is more than scoping hygiene: OAR 437-004-1131 puts the water, shade
	and rest obligations on a NAMED responsible person and FSMA §112.161(b) asks
	that person to sign the close. The phone in the hand at the start of the shift
	is that person. A body key naming somebody else would put another human's name
	against obligations they did not know they had.

	`farm_location_gps` TAKES A TYPED PLACE OVER A FIX, exactly as a completion
	does — `_location` is shared rather than re-argued. It matters more here: a
	shift with no coordinates gets no weather timeline at all, and a heat-illness
	defence built on a point-in-time temperature is not a defence.

	The crew may be empty and that is not an error. A foreman opening a shift
	before the pickers arrive is the ordinary case; `add_worker_to_shift` rosters
	them as they turn up, and the tool's own answer says so.
	"""
	allowed = guard.require_scope(user)
	foreman = _employee(user)

	inner = {
		"foreman": foreman,
		"company": _company(user, company, allowed),
		"crew_employees": _crew(crew_employees, allowed),
	}
	for key, value in (
		("location", location),
		("shift_type", shift_type),
		("start_datetime", start_datetime),
	):
		if value is not None:
			inner[key] = value
	gps = _location(farm_location_gps, latitude, longitude)
	if gps:
		inner["farm_location_gps"] = gps

	result = shifts.start_shift(inner)
	return result.data


# ── 30. add_worker_to_shift ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("add_worker_to_shift", mutating=True, limit=guard.WRITE_LIMIT)
def add_worker_to_shift(user: str, shift=None, employee=None, role=None, joined_at=None, notes=None) -> dict:
	"""Roster a late arrival onto a shift that is already running.

	`joined_at` DEFAULTS TO NOW IN THE TOOL and is forwarded when sent, because a
	phone that queued the clock-in offline knows a truer time than the moment its
	sync landed. It is the start of that person's own Attendance span when the
	shift closes, so a value half an hour late is half an hour of somebody's day.

	A shift that is already closed, a second row for somebody already on the crew,
	and a worker employed by another entity are all refused by the tool with their
	own sentences — the second of those is the one that would otherwise become two
	Attendance days for one person.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_SHIFT, shift, "shift", allowed)
	person = _employee_argument(employee, allowed)

	inner = {"shift": name, "employee": person}
	for key, value in (("role", role), ("joined_at", joined_at), ("notes", notes)):
		if value is not None:
			inner[key] = value

	result = shifts.add_worker_to_shift(inner)
	return result.data


# ── 31. end_shift ───────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("end_shift", mutating=True, limit=guard.WRITE_LIMIT)
def end_shift(
	user: str,
	shift=None,
	farm_shift=None,
	end_datetime=None,
	supervisor_signature_file_token=None,
	reviewed_on=None,
	foreman_notes=None,
) -> dict:
	"""Close a shift with the supervisor's signature, and write the crew's payroll rows.

	THE SIGNATURE IS A FILE TOKEN AND THE TOOL WILL NOT CLOSE WITHOUT ONE. It is
	the docname `finalize_staged_file` handed back after the phone uploaded the
	drawn signature in chunks — the same token `complete_task_via_mobile` carries
	for its evidence, resolved the same way. An unsigned close is an UPDATE
	setting a timestamp; §112.161(b) asks for a review that is dated AND signed.

	The close is what writes one Attendance record per crew member, each spanning
	that person's own joined_at to their own left_at. It happens once: the tool
	refuses a shift that is already closed rather than writing a second set.

	`farm_shift` IS ACCEPTED AS A SECOND SPELLING OF `shift`, v0.96.0. It is the
	name the dispatch surface already uses — `assign_farm_task` and
	`create_farm_task` have taken both since v0.72.0 — and it is the column the
	Farm Task and the Attendance row actually carry, so a client that learnt the
	word there sent it here too. `routes.bind` reduces a body to the keys the
	method DECLARES, so an undeclared `farm_shift` was dropped before any guard
	saw it and the close came back saying the argument was required while it sat
	in the body. Declaring it is what makes the refusal honest either way.

	THE TWO SPELLINGS DISAGREEING IS REFUSED rather than resolved, by
	`_one_spelling`: one of them names a shift that is not being closed, and
	nothing in the body says which. Attendance for a whole crew is written off
	this call and guessing is not available.
	"""
	allowed = guard.require_scope(user)
	named_shift, shift_label = _one_spelling(shift, farm_shift, "shift", "farm_shift")
	name = guard.require_scoped_doc(FARM_SHIFT, named_shift, shift_label, allowed)

	inner = {"shift": name}
	for key, value in (
		("end_datetime", end_datetime),
		("supervisor_signature_file_token", supervisor_signature_file_token),
		("reviewed_on", reviewed_on),
		("foreman_notes", foreman_notes),
	):
		if value is not None:
			inner[key] = value

	result = shifts.end_shift(inner)
	return result.data


# ── 32. get_i9_form ─────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_i9_form", limit=guard.READ_LIMIT)
def get_i9_form(user: str, employee=None, docname=None) -> dict:
	"""The whole I-9 back, for the screen that shows what was collected.

	THE ONLY WAY A HANDSET COULD READ AN I-9 BEFORE THIS WAS TO HAVE JUST WRITTEN
	ONE. `create_i9_form`, `submit_i9_section_1`, `submit_i9_section_2` and
	`reverify_i9` each hand back the record they changed, and `get_employee`
	reports a one-word status — so a foreman who opened the wizard on somebody
	already verified could be told `Verified` and nothing else. Which documents?
	Examined by whom? Is there a receipt still owed? All on the server, none of it
	reachable, and every one of those is a question an audit asks first.

	THE SSN IS THE LAST FOUR AND NOTHING ELSE, which is not this wrapper's doing:
	`i9._i9_fields` does not list `ssn_full` and argues at length why it never
	will. The encrypted nine digits are read in exactly one place in this app —
	`render_i9_pdf`, behind two switches — and not here.

	THE HR ROLE IS REQUIRED FOR ANYBODY ELSE'S RECORD AND NOT FOR THE CALLER'S
	OWN, the same exception `get_employee` carries and for the same reason: a
	worker reading their own I-9 is reading their own immigration paperwork, not
	the personnel file. `_employee` resolves the caller through `Employee.user_id`
	and nothing in the body, so the exception cannot be claimed by naming
	somebody else.

	READING IS LOGGED. `i9.get_i9_form` writes a `Viewed` row to the I-9 Audit
	Log on every call, which is the whole point of that log: who looked at this
	person's immigration status, when, and from where.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee or docname, allowed)

	if person != fieldwork._employee_for(user):
		personnel.require_hr_role()

	result = i9.get_i9_form({"employee": person})
	data = dict(result.data)

	# ── WHAT THIS SITE WANTS COLLECTED, SO THE APP CAN STOP GUESSING ────
	#
	# v0.136.0. Two booleans OFF I-9 SETTINGS, not off this worker's record —
	# they are facts about the employer's own policy, and the app needs them
	# BEFORE it decides what to put on the wire. `OnboardingI9Section1.apiParams`
	# sends four SSN digits with a comment saying it holds all nine and that
	# "a handset cannot read" whether the site wants them, "so sending nine
	# digits to find out would be transmitting the most sensitive number on the
	# form on the chance the site wanted it". That reasoning was right and the
	# missing read is what forced it: `store_full_ssn` was reachable only through
	# the MCP `get_i9_settings` tool, which no phone calls.
	#
	# So a farm that switched full-SSN storage on — the deliberate act of saying
	# "we run E-Verify and we keep the whole number" — could never be given one
	# by the app that collects it, and `ssn_full` stayed empty on every
	# phone-filed form on every site. `submit_i9_section_1` takes `ssn` from this
	# release; this is how the app knows whether to send it.
	#
	# NEITHER IS A SECRET AND NEITHER NAMES ANYBODY. They are two switches on a
	# Single doctype describing what the employer does, readable by any caller
	# who already got this far — which is the worker themselves or somebody
	# holding the HR role, both gated above.
	data["site_policy"] = _i9_site_policy()
	return data


def _i9_site_policy() -> dict:
	"""The two I-9 Settings switches a client has to know before it posts.

	NEVER RAISES, and returns the CONSERVATIVE answer when it cannot find out. A
	site mid-migrate answers `store_full_ssn: false`, which makes the app send
	four digits — the behaviour every release before v0.136.0 had. Guessing
	`true` on a failed read would have a handset transmit nine digits to a site
	that may never have asked for them, which is the one error worth ruling out
	by construction.
	"""
	try:
		policy = i9.get_i9_settings({}).data
	except Exception:  # pragma: no cover - a site whose Single has not migrated
		return {"store_full_ssn": False, "enrolled_in_e_verify": False}
	return {
		"store_full_ssn": bool(policy.get("store_full_ssn")),
		"enrolled_in_e_verify": bool(policy.get("enrolled_in_e_verify")),
	}


# ── 33. generate_i9_pdf ─────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("generate_i9_pdf", mutating=True, limit=guard.WRITE_LIMIT)
def generate_i9_pdf(
	user: str,
	employee=None,
	docname=None,
	overwrite=None,
	additional_information=None,
	include_full_ssn=None,
) -> dict:
	"""Fill the federal form from the record and hand the phone a URL for it.

	THE END OF THE ONBOARDING FLOW, and the step that was missing from it. The
	wizard collects Section 1 and Section 2 in an orchard and then has nothing to
	show for it: what an employer has to be able to produce for an inspection
	under 8 U.S.C. §1324a(b)(3) is Form I-9, and until v0.47.1 the only artefact
	this app made was a doctype. `i9.render_i9_pdf` writes the collected values
	into the USCIS fillable PDF this app ships and attaches it privately to the
	record; this hands back `file_url`, which is what the app opens, prints and
	hands to the two people who have to sign it.

	`include_full_ssn` IS ACCEPTED FROM v0.136.0, AND THE ARGUMENT THAT REFUSED IT
	IS WORTH RESTATING BECAUSE IT WAS HALF RIGHT. It said the number needs "a
	decision about the site's own retention policy that belongs to an operator
	with the Desk in front of them rather than to whoever is holding the handset",
	and concluded that the phone must therefore never ask. The first half is
	correct and the conclusion did not follow: THAT DECISION IS `store_full_ssn`
	IN I-9 SETTINGS, it is made in the Desk by an operator, and the tool has
	refused every caller without it since v0.47.0. A flag on this call is not a
	second policy — it is the caller saying which of the two pages they want, and
	a site that never switched storage on has no nine digits to print either way.

	WHAT THE OLD SHAPE ACTUALLY PRODUCED was the bug this release exists to fix.
	"The rendered page leaves the box empty and the employee writes the number on
	it, which is how the paper form has always worked" describes a page that gets
	PRINTED and signed with a pen. The app now seals and flattens the PDF with the
	captured signatures stamped into the page content, so nobody is writing
	anything on it afterwards — the employee typed all nine digits into the wizard
	and went back to work, and what came out the other end was a federal form with
	nine empty cells that an operator read as "the SSN was never collected".

	THREE GATES, ALL OF THEM ALREADY HERE. `require_hr_role` above; the site's
	`store_full_ssn`, enforced by `i9._full_ssn`, which REFUSES rather than
	silently blanks when the caller asks and the site does not keep them; and the
	read is written into the audit row with `full_ssn: true`, because a page
	carrying somebody's Social Security number is an event a retention audit
	should be able to find. Omitted or false, this is the page it always was —
	and `_ssn_lines` now prints the last four and the reason the box is blank into
	Additional Information, so an unset flag no longer produces a page that says
	nothing about a number the record holds.

	`overwrite` IS FORWARDED, because the wizard's realistic second call is the
	one after a correction — a misspelled name, a document number typed wrong —
	and refusing it would leave the phone holding a stale PDF with no way to ask
	for a fresh one. The File that was there stays attached to the record either
	way, so nothing is lost by re-rendering.

	EVERY REFUSAL IS THE TOOL'S: that a Destroyed I-9 is not re-rendered, that
	the site needs pypdf and the shipped template, that a second render without
	`overwrite` is refused. None of it is restated here.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee or docname, allowed)
	personnel.require_hr_role()

	inner = {"employee": person}
	if overwrite is not None:
		inner["overwrite"] = overwrite
	if additional_information is not None:
		inner["additional_information"] = additional_information
	if include_full_ssn is not None:
		inner["include_full_ssn"] = include_full_ssn

	result = i9.render_i9_pdf(inner)
	data = result.data
	return {
		"name": data.get("name"),
		"employee": data.get("employee"),
		"employee_name": data.get("employee_name"),
		"status": data.get("status"),
		"file_url": data.get("file_url"),
		"file_name": data.get("file_name"),
		"bytes": data.get("bytes"),
		"edition": data.get("edition"),
		"incomplete": data.get("incomplete") or [],
		"reverifications_not_on_page": data.get("reverifications_not_on_page") or 0,
		"replaced": data.get("replaced"),
		# WHETHER THE NINE DIGITS WENT ON THE PAGE, reported rather than assumed
		# from what was asked for. A caller that passed the flag to a site whose
		# `store_full_ssn` is off is refused outright by the tool, so `false` here
		# means the flag was not passed — and the app showing "SSN: on file, not
		# printed" is telling the truth about the artefact it is holding.
		"full_ssn_printed": bool(data.get("full_ssn_printed")),
		"note": data.get("note"),
	}


# ── 34. upload_signed_i9 ────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("upload_signed_i9", mutating=True, limit=guard.UPLOAD_LIMIT)
def upload_signed_i9(user: str, employee=None, docname=None, file_token=None, overwrite=None) -> dict:
	"""File the photographed or scanned signed copy against the I-9 record.

	THE OTHER HALF OF `generate_i9_pdf`, and the half that is the federal record.
	The rendered page is printed, the employee signs Section 1 and the verifier
	signs Section 2 — with a pen, because 8 CFR 274a.2(h) has requirements a name
	typed into a PDF does not meet — and the phone photographs the signed sheet.
	That photograph is what an inspection is shown.

	IT TAKES A `file_token`, NOT BYTES. The photograph goes up through
	`stage_file_chunk` / `finalize_staged_file` exactly like the evidence on a
	completed task and the signature on a closed shift: 512 KB at a time, hashed
	at capture and verified on assembly, resumable over a thin field link. This
	call names the File that upload produced and attaches it. A second upload
	path taking a base64 body would have its own size limit and its own way of
	failing halfway up a hill.

	`upload_id` IS NOT AN ARGUMENT AND `finalize_staged_file` IS NOT CALLED FOR
	YOU. The app already finalises its own uploads and already holds the token;
	doing it again here would mean this endpoint owning a staging session it did
	not open, and a partial upload would fail inside a call that says it is
	filing an I-9.

	THE HR ROLE IS REQUIRED WITH NO EXCEPTION — not even for the caller's own
	record. `get_i9_form` lets a worker read their own I-9 because reading it
	harms nobody; this WRITES the document the employer will be inspected on, and
	an account that could file its own signed I-9 could file one nobody signed.

	Every other refusal is the tool's: a Destroyed I-9, a file that is not a scan,
	a second signed copy without `overwrite`. The File is made private on the way
	in whatever it was.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee or docname, allowed)
	personnel.require_hr_role()

	if not str(file_token or "").strip():
		frappe.throw(
			"file_token is required — upload the signed copy with stage_file_chunk and "
			"finalize_staged_file first, then send the token that returns.",
			frappe.ValidationError,
		)

	inner = {"employee": person, "file_token": file_token}
	if overwrite is not None:
		inner["overwrite"] = overwrite

	result = i9.attach_signed_i9(inner)
	data = result.data
	return {
		"name": data.get("name"),
		"employee": data.get("employee"),
		"employee_name": data.get("employee_name"),
		"status": data.get("status"),
		"signed_pdf": data.get("signed_pdf"),
		"file_token": data.get("file_docname"),
		"replaced": data.get("replaced"),
	}


# ── 35. list_authorized_signers ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_authorized_signers", limit=guard.READ_LIMIT)
def list_authorized_signers(user: str, include_inactive=None, form_type=None) -> dict:
	"""Who this employer has authorised to sign an I-9 or a W-4.

	THE READ THE SECTION 2 SCREEN NEEDS BEFORE IT OFFERS A NAME. v0.48.0 made
	the verifier a roster lookup rather than a free-text box, and a wizard that
	could not read the roster would have to discover its own account's
	authorisation by submitting a form and being refused — in an orchard, having
	just examined somebody's documents.

	`configured` IS THE FIELD THAT MATTERS TO THE APP. False means the site has
	no roster and the old free-text box is still correct; true means the name is
	the server's to supply and the field should be prefilled and read-only
	unless the foreman is filing on somebody else's behalf.

	THE HR ROLE IS REQUIRED. The roster names the people who can attest to a
	federal form for this business, which is not a field worker's read.
	"""
	guard.require_scope(user)
	personnel.require_hr_role()

	inner = {}
	if include_inactive is not None:
		inner["include_inactive"] = include_inactive
	if form_type is not None:
		inner["form_type"] = form_type

	result = signers.list_authorized_signers(inner)
	return result.data


# ── 36. add_authorized_signer ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("add_authorized_signer", mutating=True, limit=guard.WRITE_LIMIT)
def add_authorized_signer(
	user: str, signer_user=None, full_name=None, title=None, can_sign_i9=None, can_sign_w4=None
) -> dict:
	"""Put one account on the roster from the app.

	`signer_user` RATHER THAN `user`, and the rename is not cosmetic.
	`guard.endpoint` injects the AUTHENTICATED caller into `user` and
	`routes.accepted_arguments` drops any `user` a body carries, precisely so an
	account cannot name somebody else in a request. The person being authorised
	is a different argument and has to have a different name, or it would be
	dropped on the way in and this endpoint would silently authorise the caller.

	`active` IS NOT ACCEPTED. Adding somebody inactive is a configuration state
	with no meaning on a phone — the app adds a signer in order to let them sign.
	`update_authorized_signer` and `remove_authorized_signer` are what change it
	afterwards.

	EVERY REFUSAL IS THE TOOL'S: an account that is not on the site, a second row
	for one account, a full name that can be found nowhere. So is the warning
	that this row was the first and has just turned enforcement on for the whole
	site — which the app should show, because the next foreman to file a Section
	2 is the one it affects.
	"""
	guard.require_scope(user)
	personnel.require_hr_role()

	inner = {"user": signer_user}
	for key, value in (
		("full_name", full_name),
		("title", title),
		("can_sign_i9", can_sign_i9),
		("can_sign_w4", can_sign_w4),
	):
		if value is not None:
			inner[key] = value

	result = signers.add_authorized_signer(inner)
	return result.data


# ── 37. update_authorized_signer ────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("update_authorized_signer", mutating=True, limit=guard.WRITE_LIMIT)
def update_authorized_signer(
	user: str, signer_user=None, full_name=None, title=None, can_sign_i9=None, can_sign_w4=None, active=None
) -> dict:
	"""Change one signer's printed name, title, or what they may sign.

	`signer_user` for the same reason as above. `active` IS accepted here, and
	it is the only way back from `remove_authorized_signer` — a roster with
	nobody active refuses every Section 2 on the site, so the call that undoes
	that has to be reachable from wherever the call that caused it was made.
	"""
	guard.require_scope(user)
	personnel.require_hr_role()

	inner = {"user": signer_user}
	for key, value in (
		("full_name", full_name),
		("title", title),
		("can_sign_i9", can_sign_i9),
		("can_sign_w4", can_sign_w4),
		("active", active),
	):
		if value is not None:
			inner[key] = value

	result = signers.update_authorized_signer(inner)
	return result.data


# ── 38. remove_authorized_signer ────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("remove_authorized_signer", mutating=True, limit=guard.WRITE_LIMIT)
def remove_authorized_signer(user: str, signer_user=None) -> dict:
	"""Deactivate one signer. The row is kept — see `tools/signers.py`.

	NOTHING IS DELETED HERE OR ANYWHERE. A form signed last season was signed by
	whoever was authorised last season, and the tool clears a flag rather than
	dropping a row so that stays answerable. The result carries the warning when
	this call left the site with no active signers at all, which is a state that
	refuses every subsequent Section 2 — the app should surface it rather than
	let the next foreman find out in a field.
	"""
	guard.require_scope(user)
	personnel.require_hr_role()

	result = signers.remove_authorized_signer({"user": signer_user})
	return result.data


# ── 39. generate_w4_pdf ─────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("generate_w4_pdf", mutating=True, limit=guard.WRITE_LIMIT)
def generate_w4_pdf(user: str, employee=None, docname=None, tax_year=None, overwrite=None) -> dict:
	"""Fill the federal W-4 from the record and hand the phone a URL for it.

	THE OTHER HALF OF `generate_i9_pdf`, and the last artefact the onboarding
	flow was missing. The wizard has collected withholding elections since
	v0.45.0 and had nothing to show for them: what an employer keeps for an
	employee's withholding is Form W-4, and until v0.48.0 the only thing this app
	produced was a doctype. `w4.render_w4_pdf` writes the collected values into
	the IRS fillable PDF this app ships and attaches it privately to the record;
	this hands back `file_url`, which is what the app opens, prints and hands to
	the employee to sign.

	THE EMPLOYER BLOCK NEEDS NOTHING FROM THE PHONE. Step 5's employer name,
	address, EIN and first date of employment are resolved on the server from
	I-9 Settings, the Company and `Employee.date_of_joining` — so a foreman in an
	orchard is not typing an EIN into a handset, which is the failure mode that
	would put a wrong one on a federal form.

	`overwrite` IS FORWARDED, because the wizard's realistic second call is the
	one after a correction — a filing status picked wrong, a dependent count off
	by one — and refusing it would leave the phone holding a stale PDF with no
	way to ask for a fresh one. The File that was there stays attached either way.

	THE HR ROLE IS REQUIRED. A W-4 names a person's filing status, their
	dependents and their other income; it is a payroll record and not a picker's
	to render.

	EVERY REFUSAL IS THE TOOL'S: that the site needs pypdf and the shipped
	template, that a second render without `overwrite` is refused, that there is
	no active W-4 for this person. None of it is restated here.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee or docname, allowed)
	personnel.require_hr_role()

	inner = {"employee": person}
	if tax_year is not None:
		inner["tax_year"] = tax_year
	if overwrite is not None:
		inner["overwrite"] = overwrite

	result = w4.render_w4_pdf(inner)
	data = result.data
	return {
		"name": data.get("name"),
		"employee": data.get("employee"),
		"employee_name": data.get("employee_name"),
		"tax_year": data.get("tax_year"),
		"status": data.get("status"),
		"file_url": data.get("file_url"),
		"file_name": data.get("file_name"),
		"bytes": data.get("bytes"),
		"edition": data.get("edition"),
		"template_tax_year_matches": data.get("template_tax_year_matches"),
		"incomplete": data.get("incomplete") or [],
		"replaced": data.get("replaced"),
		"note": data.get("note"),
	}


# ── 40. attach_onboarding_document ──────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("attach_onboarding_document", mutating=True, limit=guard.UPLOAD_LIMIT)
def attach_onboarding_document(
	user: str, employee=None, docname=None, file_token=None, document_kind=None
) -> dict:
	"""File an uploaded photograph or signature against the Employee record.

	v0.48.3. THE CALL WHOSE ABSENCE LOST EVERY PIECE OF ONBOARDING EVIDENCE.
	The wizard collects six files — the Section 1 signature, the List A or List
	B/C document photographs, the Section 3 signature and the photographed W-4 —
	and this surface published no way to file any of them. So the app sent them
	to Frappe's own `/api/method/upload_file`, which is not one of this app's
	paths, which means `fallback_auth._is_mobile_path` never looked at the
	`X-FarmOps-Token` header, which means the funnel-stripped request arrived as
	Guest and Frappe answered 200 with the Desk login page. The app checked the
	status and not the body, so the wizard advanced and the I-9 read Complete
	with nothing behind it. That is the failure this endpoint ends, and the
	§1324a(b)(3) inspection is the reason it mattered.

	IT TAKES A `file_token`, NOT BYTES — the docname `finalize_staged_file` hands
	back, exactly like `upload_signed_i9` and `complete_task_via_mobile`. The
	photograph goes up in 512 KB slices, hashed at capture and verified on
	assembly, and this call names what that produced. There is now ONE upload
	path from this app and it is the one that authenticates.

	WHY NOT JUST LET `finalize_staged_file` ATTACH. Because it deliberately
	refuses an attachment target: `api/files.py` argues it at length, and the
	argument is that a field worker who could name the parent could hang a file
	off a Journal Entry or somebody else's lease. This endpoint names ONE parent
	doctype, in code, and proves the docname is an Employee inside the caller's
	own entities before it goes near the File.

	A HIRING ROLE IS REQUIRED, AND STILL NEVER THE CALLER'S OWN RECORD. These are
	the photographs an employer is inspected on, and an account that could file
	its own would be an account that could file anything as its own identity
	documents — so the self-service exception other reads get is deliberately
	absent here.

	v0.94.0 MOVED THIS FROM `require_hr_role`, and the reason is the flow rather
	than the file: this is step 7 of a hire, between the W-4 and the bunk. The
	person holding the phone that photographed the licence is the person sitting
	with the new hire, and a gate that made them hand the phone to an HR account
	that does not exist on this farm is a gate that loses the evidence — which is
	the exact failure v0.48.3 built this endpoint to end. What protects the
	photographs is not the role: `_employee_argument` proves the record is inside
	this caller's entities, ONE parent doctype is named in code, and the token
	came from an authenticated upload that was hashed at capture.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee or docname, allowed)
	personnel.require_hiring_role()

	if not str(file_token or "").strip():
		frappe.throw(
			"file_token is required — upload the photograph with stage_file_chunk and "
			"finalize_staged_file first, then send the token that returns.",
			frappe.ValidationError,
		)

	inner = {"employee": person, "file_token": file_token}
	if document_kind is not None:
		inner["document_kind"] = document_kind

	result = personnel.attach_employee_document(inner)
	data = result.data
	return {
		"employee": data.get("employee"),
		"document_kind": data.get("document_kind"),
		"file_token": data.get("file_token"),
		"file_url": data.get("file_url"),
		"file_name": data.get("file_name"),
		"is_private": data.get("is_private"),
		"already_attached": data.get("already_attached"),
	}


# ── 41. get_active_model ────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_active_model", limit=guard.READ_LIMIT)
def get_active_model(user: str, company=None, piecework_activity=None) -> dict:
	"""Which ML model is deployed for one piecework activity, and its manifest.

	v0.52.0. THE MODEL BINARY IS NOT IN THIS ANSWER — `get_model_file_chunk` is
	the second call, made only when the manifest's `uuid` no longer matches
	whatever this app already has cached on disk. `manifest.metadata.downloadable`
	says whether that second call has anything to read yet; when it does not,
	the model is registered but `attach_model_file` has not run on this site.
	"""
	allowed = guard.require_scope(user)
	wanted = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	activity = str(piecework_activity or "").strip()
	if not activity:
		frappe.throw("piecework_activity is required.", frappe.ValidationError)

	result = ml_model_tools.get_active_model({"company": wanted, "piecework_activity": activity})
	return result.data


# ── 42. get_model_file_chunk ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("get_model_file_chunk", limit=guard.UPLOAD_LIMIT)
def get_model_file_chunk(user: str, model=None, chunk_index=None, chunk_bytes=None) -> dict:
	"""One base64 slice of an ML model's binary, in the same shape FarmOpsKit
	already streams uploads in.

	v0.52.0, AND THE WHOLE POINT OF THIS RELEASE: an iOS app reads the model
	back from HERE, through the credential it already holds, rather than
	opening a second connection to Volume Vision with a second credential —
	see `tools/ml_model.py`'s module docstring.

	`model` NAMES AN ML Model RECORD, SCOPED THE SAME WAY A TASK IS. A phone's
	own cache is keyed on `uuid` from get_active_model's manifest, which is
	`source_uuid` rather than a docname when this model came from Volume
	Vision — `_model_docname` resolves that spelling before
	`guard.require_scoped_doc` refuses one that belongs to an entity this
	caller cannot reach as not found, same as any other docname argument here.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(ML_MODEL, _model_docname(model), "model", allowed)

	result = ml_model_tools.get_model_file_chunk(
		{"model": name, "chunk_index": chunk_index, "chunk_bytes": chunk_bytes}
	)
	return result.data


# ── 43. list_onboarding_reference_data ──────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_onboarding_reference_data", limit=guard.READ_LIMIT)
def list_onboarding_reference_data(user: str, company=None) -> dict:
	"""The four dropdowns on the wizard's Assignment step, in one call.

	v0.54.0, and the same failure `list_i9_document_types` fixed for the I-9's
	document picker: a Swift array of employment types compiled into the app is a
	copy of a table an operator maintains in the Desk, and it goes stale silently.
	`create_employee` checks every one of these against THIS site's records and
	refuses a value that names nothing — so a hardcoded list is not merely stale,
	it is a wizard whose Assignment step fails at the end of a hire with "not a
	Designation on this site" and no way to find out what is.

	ONE CALL FOR FOUR LISTS, on purpose. They are read together, once, when the
	step opens, and four round trips over a tailgate LTE connection is four
	chances to half-populate a form. `list_i9_document_types` groups its answer
	for the same reason.

	A MASTER THIS SITE DOES NOT HAVE COMES BACK EMPTY AND IS NAMED IN
	`masters_absent`, never omitted and never an error. Branch, Department,
	Designation and Employment Type all ship with Frappe HR; a site without hrms
	has none of them, and the honest answer there is a wizard that offers no
	choices for that field rather than a hire that cannot start. `create_employee`
	agrees — `_clean` does not check a Link whose target doctype is absent.

	DEPARTMENTS ARE SCOPED TO THE CALLER'S ENTITIES, and the other three are not,
	because Department is the only one of the four that carries a company on a
	stock Frappe HR. Group departments are dropped: `is_group` marks a node in the
	tree rather than somewhere a person is assigned, and an Employee pointed at
	one is a report that double-counts them.

	EVERY BRANCH ROW CARRIES ITS PARCELS, and that is what makes the Housing step
	reachable from the Assignment step. An Employee carries a Branch and a Housing
	Unit stands on a Parcel; `Parcel.branch` is the only column joining the two,
	and without it a wizard that has just asked which camp somebody works at
	cannot then show that camp's cabins.

	`parcels` IS A LIST AND `parcel` IS THE SINGLE ONE WHEN THERE IS EXACTLY ONE.
	A camp is a place rather than a deed — one that grew across a fence line is
	two parcels — so the list is the real answer and the scalar is the
	convenience for the ordinary case. `parcel` is null when a branch maps to
	none OR to several, and a client that reads only the scalar must treat null as
	"ask the server", which is what passing `branch` to `list_available_housing`
	does. That endpoint resolves the same mapping through the same function, so
	the phone never has to do the lookup itself and the two can never disagree.

	A BRANCH WITH NO PARCELS IS REPORTED, NOT HIDDEN — empty `parcels`, and the
	branch still in the list. It is a real operating unit somebody may legitimately
	hire into; what it is not is a camp with housing, and `list_available_housing`
	says so in its own words rather than returning an empty list that reads as a
	full camp.

	VIEW-ONLY AND NOT A PERSONNEL READ. There is nothing on these rows about a
	person — a job title, a camp name, an employment class. `guard.endpoint` has
	run the kill switch, the role gate, the enrolment gate and the rate limit,
	which is the whole of what this needs; `search_employees`, which really does
	read the register, carries the HR role gate and this deliberately does not.

	v0.62.0 MOVED THE BODY INTO `_onboarding_reference_data` AND CHANGED NOTHING
	HERE. `list_org_reference_data` is the same read under the name the handset
	calls; it takes the same one argument, and the only reason both exist is that
	a phone already in an orchard must not have to be reinstalled to get an answer.
	"""
	return _onboarding_reference_data(user, company)


def _onboarding_reference_data(user: str, company) -> dict:
	"""The four dropdowns. See `list_onboarding_reference_data` for every rule."""
	allowed = guard.require_scope(user)
	wanted = guard.require_company(user, company, allowed)
	entities = [wanted] if wanted else allowed

	out: dict = {"company": wanted or None}
	absent = []
	for key, doctype, label_field in REFERENCE_MASTERS:
		if not compat.doctype_exists(doctype):
			out[key] = []
			absent.append(doctype)
			continue

		filters = {}
		if doctype == "Department":
			if compat.has_field(doctype, "company"):
				filters["company"] = ("in", entities)
			if compat.has_field(doctype, "is_group"):
				filters["is_group"] = 0

		fields = compat.existing_fields(doctype, ["name", label_field, "company"])
		rows = frappe.db.get_all(
			doctype,
			filters=filters or None,
			fields=fields,
			order_by="name asc",
			limit_page_length=REFERENCE_LIMIT,
		)
		listed = [
			{
				"name": row.get("name"),
				"label": str(row.get(label_field) or "").strip() or row.get("name"),
				"company": row.get("company") or None,
			}
			for row in rows or []
		]
		# The belt to the braces on the one master that has a company at all. It
		# runs on all four because `scoped` keeps a row with no company, so a
		# Designation is untouched and a Department that slipped the filter is not.
		out[key] = guard.scoped(listed, allowed)

	# The ground each branch holds, in ONE query for every row rather than one per
	# row. Scoped to the entities this caller may reach, so a branch that also has
	# parcels under a company they cannot see reports only the ones they can — the
	# same rule every other read on this surface follows, applied to the join
	# rather than only to the rows.
	mapping = housing_tools.branch_parcel_map([row["name"] for row in out["branches"]], wanted or entities)
	for row in out["branches"]:
		parcels = mapping.get(row["name"], [])
		row["parcels"] = parcels
		# Null for none AND for several. A scalar that silently picked the first
		# of two parcels would send half a camp's cabins missing, and a client
		# reading only this field has to fall back to asking the server — which
		# is what passing `branch` to `list_available_housing` does.
		row["parcel"] = parcels[0] if len(parcels) == 1 else None
		row["parcel_count"] = len(parcels)

	out["counts"] = {key: len(out[key]) for key, _doctype, _label in REFERENCE_MASTERS}
	out["masters_absent"] = absent
	out["branches_without_parcels"] = [row["name"] for row in out["branches"] if not row["parcels"]]
	return out


# ── the two spellings of one flag, and of one cabin ─────────────────────────
#
# v0.63.1. THE v0.62.0 ALIASES WERE REACHABLE UNDER ONE SPELLING EACH, WHICH IS
# HALF OF WHAT AN ALIAS IS FOR. That release declared `assignable_only` on
# `list_housing_units` and `unit`/`assigned_date` on `create_housing_assignment`
# because `routes.bind` reduces a body to the keys a signature names, so the
# handset's spellings could not otherwise arrive at all. What it left standing is
# the MIRROR of the bug it fixed: the same silent drop, pointed the other way. A
# caller reaching the new name with the older body loses the filter and is shown
# cabins nobody can be put in; a caller reaching the older name with the new body
# loses the cabin and the date and is refused a hire it named. Both doors now
# declare both spellings, and the three functions below are the ONE place the
# pairs are reconciled — an alias whose reconciliation lived in the wrapper would
# be a second copy of the rule the moment a third spelling arrived.
#
# NEITHER DOOR'S DEFAULT MOVED, and that is why `default_full` is an argument
# rather than a constant. `include_full` and `assignable_only` are not two names
# for one flag; they are OPPOSITE SENSES WITH OPPOSITE DEFAULTS. The older name
# answers "where can somebody sleep" and drops the full cabins and the condemned
# one; the handset's name answers "show me the camp" and keeps them, marked and
# greyed out. Accepting both spellings changes what a caller MAY send. It does
# not change what either name answers when the caller sends neither, because
# every handset already in an orchard is a caller who sends neither.
#
# A CONTRADICTION IS REFUSED RATHER THAN RESOLVED. `include_full=true` beside
# `assignable_only=true` is not a body any client this app knows about produces,
# and there is no reading of it truer than the other — so it is refused by name,
# with both keys quoted back, rather than settled in favour of whichever the code
# happens to read first. A wrong list of beds is the failure this whole block
# exists to prevent, and silently picking one half of a contradiction is that
# failure with a different cause.
def _was_sent(raw) -> bool:
	"""Whether the body actually carried this key. A literal `false` counts as sent.

	`False` and `0` are ANSWERS, and the difference between "the caller said no"
	and "the caller said nothing" is the whole of which default applies. So this
	tests presence and never truth — `str(False)` is `"False"`, which is not the
	empty string and is therefore something the caller said.
	"""
	return str(raw if raw is not None else "").strip() != ""


def _said_yes(raw) -> bool:
	"""What this surface reads as true on the wire. Absence is false, as is anything else."""
	return str(raw or "").strip().lower() in ("1", "true", "yes")


def _camp_breadth(include_full, assignable_only, default_full: bool) -> bool:
	"""One boolean out of two spellings of one flag: keep the cabins with no bed left.

	See the block above for the whole argument. `default_full` is the answering
	method's OWN default, applied when the body carried neither spelling.
	"""
	full_sent, narrow_sent = _was_sent(include_full), _was_sent(assignable_only)
	if full_sent and narrow_sent:
		# They agree only when they DISAGREE in value, because one is the negative
		# of the other. Both true, or both false, is a body that asked for the
		# camp and for the open beds in the same breath.
		if _said_yes(include_full) == _said_yes(assignable_only):
			frappe.throw(
				"include_full and assignable_only are one flag in opposite senses and this body "
				f"says both, to the same effect: include_full={include_full!r} with "
				f"assignable_only={assignable_only!r}. Send one of them. "
				"assignable_only=true and include_full=false are the same request — the cabins "
				"with a bed free tonight; assignable_only=false and include_full=true are the "
				"other one — the whole camp, the full and condemned units marked and kept. "
				"Nothing was read.",
				frappe.ValidationError,
			)
		return _said_yes(include_full)
	if full_sent:
		return _said_yes(include_full)
	if narrow_sent:
		return not _said_yes(assignable_only)
	return default_full


def _one_spelling(primary, alias, primary_label: str, alias_label: str) -> tuple:
	"""One value out of two spellings of one argument, and the name the caller used.

	THE LABEL TRAVELS WITH THE VALUE because `_house_one_person` quotes it in
	every refusal it makes, and a phone told `check_in_date is required` by a
	method it called with `assigned_date` is a phone whose operator cannot act on
	the sentence. The label returned is the spelling the BODY carried, not the
	one the door happens to prefer; the door's own spelling is the fallback for a
	body that carried neither, which is the case that produces "is required".

	TWO DIFFERENT VALUES ARE REFUSED, for the same reason a contradicting flag
	is: one of them is a cabin somebody is not being put in, and there is nothing
	in the body saying which.
	"""
	primary_value = str(primary or "").strip()
	alias_value = str(alias or "").strip()
	if primary_value and alias_value and primary_value != alias_value:
		frappe.throw(
			f"{primary_label} and {alias_label} are two spellings of one argument and this body "
			f"says both, differently: {primary_label}={primary_value} against "
			f"{alias_label}={alias_value}. Send one of them. Nothing was written.",
			frappe.ValidationError,
		)
	if primary_value:
		return primary_value, primary_label
	if alias_value:
		return alias_value, alias_label
	return "", primary_label


# ── 44. list_available_housing ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_available_housing", limit=guard.READ_LIMIT)
def list_available_housing(
	user: str,
	company=None,
	parcel=None,
	branch=None,
	include_full=None,
	employee=None,
	assignable_only=None,
) -> dict:
	"""Which cabins have a bed free tonight, and how full each one already is.

	v0.54.0. The wizard's Housing step, and the read `assign_housing` exists to be
	the write for. Beds and bodies per unit, so a foreman standing at a tailgate
	can put somebody somewhere without walking the camp or opening the Desk.

	IT COUNTS OCCUPANTS AND DOES NOT NAME THEM. `list_housing_units` returns an
	`occupants` list of employee names — who sleeps in which cabin, which is a
	personnel fact and is exactly the sort of thing that has no business on a
	picker's phone merely because the vacancy count does. The count is what fills
	a dropdown; the names are read in the Desk, or through
	`get_employee_housing_history` on the MCP console. That split is the reason
	this read carries no HR role gate where `search_employees` does.

	`employee` IS THE ONE ARGUMENT THAT CROSSES THAT LINE, AND IT CARRIES THE GATE
	WITH IT. Passing it returns `previous_assignment` — where this person slept
	last season, so the wizard can offer "Last year: MC-Cabin-07" at the top of
	the list and a returning picker is one tap instead of a scroll through forty
	cabins nobody remembers the numbers of.

	That is exactly the fact the paragraph above keeps off this endpoint: a named
	person, a named cabin, and the dates between them. So a role gate runs WHEN AND
	ONLY WHEN `employee` is passed. Without it, this method would be a way to walk
	the housing register one employee docname at a time — the same register
	`search_employees` guards — reachable by anybody holding a picker's phone. The
	vacancy read stays open to a Field Worker because it still names nobody.

	v0.94.0: THAT GATE IS `require_hiring_role`, NOT `require_hr_role`. Tim names
	"assign a bunk" as a foreman step, and the whole value of this argument is the
	one tap it saves on a returning picker — "Last year: MC-Cabin-07" at the top of
	the list, on the phone of the person actually walking them to a cabin. The
	previous docstring justified the HR gate by observing that the onboarding phone
	"is enrolled as a Farm Manager already", which was true of one configured
	handset and is not a reason the check has to demand it.

	IT READS ONLY *ENDED* ASSIGNMENTS, most recent first. A returning worker is by
	definition somebody whose last stay finished; an open assignment means they are
	housed RIGHT NOW, and offering "last year: Cabin 7" to somebody currently in
	Cabin 7 is an offer to double-book them. `currently_housed` says so instead, so
	the wizard can show that rather than a stale preference.

	`previous_assignment.available` IS COMPUTED FOR THE UNIT ITSELF, not read off
	the list above. The list is filtered — by branch, and by the default that drops
	full and condemned units — so a cabin that is missing from it is exactly the
	case this field has to answer for, and looking the answer up in a list that
	dropped it would report every full cabin as available.

	NON-RESIDENTIAL UNITS ARE NOT IN THE ANSWER AT ALL. A shower block and a shop
	are Housing Units with a capacity of zero, `create_housing_assignment` refuses
	an assignment into either by name, and a dropdown offering them is a dropdown
	whose next screen is a refusal. Uninhabitable units are LISTED and marked
	`assignable: false` with the reason — a foreman who cannot find the cabin they
	expected needs to be told it is condemned, not shown a shorter list.

	`include_full=true` KEEPS THE UNITS WITH NO BED LEFT, marked the same way. The
	default drops them, because the question this answers is "where can somebody
	sleep"; the flag is for the screen that shows the whole camp.

	`branch` RESOLVES TO ITS PARCELS SERVER-SIDE, so the phone passes the camp it
	just hired somebody into and gets that camp's cabins back. A Housing Unit
	stands on a Parcel and carries no Branch of its own — a person REPORTS to a
	branch, a cabin STANDS ON ground somebody owns — and `Parcel.branch` (v0.54.0)
	is the column joining the two. It is resolved through
	`housing.parcels_for_branch`, which is the same function
	`list_onboarding_reference_data` fills its `parcels` field from, so the
	mapping the wizard was shown and the mapping this filters on cannot disagree.

	A BRANCH MAY HOLD SEVERAL PARCELS and every one of them is included. A camp
	that grew across a fence line is two parcels, and a filter that took only the
	first would hide half the beds on exactly the operations big enough to have
	the problem.

	THE THREE WAYS THIS CAN FAIL ARE THREE DIFFERENT ANSWERS, and none of them is
	a silent empty list — an empty camp reads on a phone as "no room", which is
	the one wrong answer here:

	  * the branch names no Branch record → REFUSED, naming it. A typo resolves to
	    no parcels and would otherwise look exactly like a full camp.
	  * the branch is real but no parcel carries it → the whole list, with
	    `branch_filter_applied: false` and `branch_note` saying that no ground is
	    tagged with this branch and that `update_parcel(branch=…)` is the fix.
	  * this site has no `Parcel.branch` column yet (not migrated) → the same,
	    with `branch_note` naming the migration.

	`parcel` IS STILL ACCEPTED and is narrower than `branch`. Passing both filters
	to the intersection, which is what somebody asking for one parcel of a
	two-parcel camp means.

	v0.62.0 MOVED THE BODY INTO `_available_housing` AND CHANGED NOTHING HERE.
	`list_housing_units` is the same read under the name the handset calls and the
	one argument it spells differently; every paragraph above is a rule about what
	a phone may see of a camp, and two copies of them would be two answers to the
	same question a season from now.

	v0.63.1 ACCEPTS `assignable_only` HERE TOO, AND THE DEFAULT ABOVE IS UNMOVED.
	It is the negative of `include_full` — `assignable_only=true` is this method's
	default question and `assignable_only=false` is the whole camp — and it is
	declared because `routes.bind` drops what a signature does not name, so a
	client that learned the handset's spelling was silently getting the default
	from this door rather than the filter it sent. Sending BOTH spellings to the
	same effect is refused by name rather than resolved; see `_camp_breadth`.
	"""
	return _available_housing(
		user,
		company=company,
		parcel=parcel,
		branch=branch,
		# False: this method's question is "where can somebody sleep", so a body
		# that names neither spelling drops the full cabins and the condemned one.
		include_full=_camp_breadth(include_full, assignable_only, default_full=False),
		employee=employee,
	)


def _available_housing(user: str, company, parcel, branch, include_full, employee) -> dict:
	"""The camp read both wrappers make. See `list_available_housing` for every rule."""
	allowed = guard.require_scope(user)
	wanted = guard.require_company(user, company, allowed)
	compat.require_doctype(
		HOUSING_UNIT,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)

	# The gate rides with the argument. See the docstring: everything else this
	# method returns is a building and a bed count, and this one thing is a named
	# person's housing history.
	previous = None
	if str(employee or "").strip():
		personnel.require_hiring_role()
		previous = _previous_assignment(
			guard.require_scoped_doc(EMPLOYEE, employee, "employee", allowed), allowed
		)

	branch_wanted = str(branch or "").strip()
	branch_parcels: list = []
	branch_note = None
	if branch_wanted:
		# A branch that names nothing is refused BEFORE the register is read. It
		# resolves to no parcels, and "no parcels" and "no beds" produce the same
		# empty list from here on — so the mistake has to be caught while it can
		# still be told apart from an answer.
		if compat.doctype_exists("Branch") and not frappe.db.exists("Branch", branch_wanted):
			frappe.throw(
				f"branch {branch_wanted} is not one on this site. "
				"list_onboarding_reference_data has the branches, each with the parcels it "
				"holds. Nothing was read.",
				frappe.DoesNotExistError,
			)
		if not compat.has_field("Parcel", "branch"):
			branch_note = (
				"This site's Parcel doctype has no branch column, so a branch cannot be "
				"resolved to the ground it holds. Run `bench --site <site> migrate` after "
				"upgrading to v0.54.0. Every unit is listed rather than none."
			)
		else:
			branch_parcels = housing_tools.parcels_for_branch(branch_wanted, wanted or allowed)
			if not branch_parcels:
				branch_note = (
					f"No parcel is tagged with branch {branch_wanted}, so there is no ground "
					"to look for housing on. Set it with update_parcel(parcel=..., "
					f"branch='{branch_wanted}'). Every unit is listed rather than none."
				)

	inner = {"limit": HOUSING_LIST_LIMIT}
	if wanted:
		inner["company"] = wanted
	if str(parcel or "").strip():
		inner["parcel"] = str(parcel).strip()

	result = housing_tools.list_housing_units(inner)

	branch_applied = bool(branch_parcels)
	permitted_parcels = set(branch_parcels)

	show_full = str(include_full or "").strip().lower() in ("1", "true", "yes")
	units = []
	for unit in result.data.get("units") or []:
		if not unit.get("residential"):
			continue
		if branch_applied and unit.get("parcel") not in permitted_parcels:
			continue

		capacity = int(unit.get("capacity") or 0)
		occupants = int(unit.get("currently_assigned") or 0)
		# A unit nobody has given a capacity is NOT reported as full. Zero here
		# means unmeasured, which `lawful_occupancy` produces for a cabin with no
		# floor area on file, and a camp whose capacities were never entered would
		# otherwise come back with every bed taken and no way to tell why.
		open_beds = max(0, capacity - occupants) if capacity else None
		condemned = unit.get("condition") == "Uninhabitable"
		full = bool(capacity) and occupants >= capacity

		if not show_full and (full or condemned):
			continue

		reason = None
		if condemned:
			reason = "Marked Uninhabitable. It has to be repaired and inspected before anybody is put in it."
		elif full:
			reason = f"All {capacity} bed(s) are taken."

		units.append(
			{
				"name": unit.get("name"),
				"unit_name": unit.get("unit_name"),
				"unit_type": unit.get("unit_type"),
				"parcel": unit.get("parcel"),
				"parcel_name": unit.get("parcel_name") or unit.get("parcel"),
				"company": unit.get("owning_entity"),
				"capacity": capacity or None,
				"current_occupants": occupants,
				# v0.62.0. The same count under the key `HousingUnit` decodes.
				# Two spellings of one number rather than a rename, because a
				# handset already in the field reads the first.
				"occupied": occupants,
				"open_beds": open_beds,
				"status": "Uninhabitable" if condemned else ("Full" if full else "Available"),
				"condition": unit.get("condition"),
				"assignable": not (condemned or full),
				"unassignable_reason": reason,
				"blocked_reason": reason,
				# ALWAYS TRUE, AND STATED RATHER THAN OMITTED. The loop above
				# drops every non-residential unit before it gets here — see the
				# docstring on why a dropdown must not offer a shower block — so
				# every row that survives is somewhere a person sleeps. The
				# handset's own fallback guesses this from `unit_type` when the
				# server is silent, and a guess about a customised type would
				# grey out a real cabin.
				"is_residential": True,
				"max_occupants_per_or_law": unit.get("max_occupants_per_or_law"),
				"capacity_over_lawful_occupancy": unit.get("capacity_over_lawful_occupancy"),
				"inspection_overdue": unit.get("inspection_overdue"),
				"gps": unit.get("gps"),
			}
		)

	units = guard.scoped(units, allowed)
	open_beds = sum(unit["open_beds"] or 0 for unit in units)
	return {
		"units": units,
		"count": len(units),
		"assignable_count": sum(1 for unit in units if unit["assignable"]),
		"open_beds": open_beds,
		# v0.62.0. `HousingUnitList` reads `total_open_beds` first and `open_beds`
		# second, and the number is the same one — the step's header says "6 beds
		# open across 4 cabins" and computing that from the rows it was shown
		# would be wrong by exactly the filter that was applied.
		"total_open_beds": open_beds,
		"previous_assignment": previous,
		"company": wanted or None,
		"parcel": str(parcel or "").strip() or None,
		"branch": branch_wanted or None,
		"branch_filter_applied": branch_applied,
		# The ground the branch resolved to, echoed back. A foreman looking at an
		# unexpectedly short list needs to see which parcels were searched, and a
		# client that wants to cache the mapping gets it here rather than making a
		# second call for it.
		"branch_parcels": branch_parcels,
		"branch_note": branch_note,
		"include_full": show_full,
	}


# ── 45. assign_housing ──────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("assign_housing", mutating=True, limit=guard.WRITE_LIMIT)
def assign_housing(
	user: str,
	employee=None,
	housing_unit=None,
	check_in_date=None,
	end_date=None,
	deposit_paid=None,
	housing_deduction_from_wages=None,
	notes=None,
	unit=None,
	assigned_date=None,
) -> dict:
	"""Put one new hire in one cabin from one date. The wizard's Housing step.

	v0.54.0. IT DELEGATES to `create_housing_assignment`, so the overlap rule, the
	refusal of a shower block, the refusal of a condemned unit, the deposit
	arithmetic and the Section 119 note are all the code an operator gets on the
	MCP console — the reason `create_employee` delegates to `tools/employee.py`.

	IT CARRIES THE HR ROLE GATE. A Housing Assignment names a person, a building
	and the dates between them; it is the audit trail defending an IRS Section 119
	exclusion and the record a wage claim about an ORS 653 housing deduction is
	answered from. That is a personnel record, and the same gate `search_employees`
	applies by hand applies here for the same reason — a picker holding a perfectly
	good Farm Ops grant is refused, and the operator enrolling an onboarding phone
	enrols it as a Farm Manager.

	IT REFUSES TO OVERFILL A CABIN WHERE THE TOOL ONLY WARNS, and that difference
	is deliberate rather than an oversight in one of them. `create_housing_assignment`
	reports "now holds 5 against a recorded capacity of 4" in `warnings` and writes
	the row, which is right on a console where an operator can see the warning,
	weigh it and mean it — a barracks really does take a fifth bunk some seasons.
	It is wrong on a phone: nothing on the Housing step displays a warning, the
	foreman has already walked away, and a bed that does not exist becomes somebody
	sleeping in a truck. So the count is taken BEFORE the write and the refusal
	names the unit, its capacity and who is already in it.

	`allow_multi_occupancy` IS NOT FORWARDED, and the wizard cannot send it. Under
	capacity this method passes it as true on the caller's behalf — a four-bunk
	cabin with one person in it is the ordinary case and the tool refuses a second
	assignment without the flag — and AT capacity the check above has already
	refused. Letting a phone send the flag itself would hand it the one argument
	that turns the capacity refusal off.

	v0.62.0 MOVED THE BODY INTO `_house_one_person` AND CHANGED NOTHING HERE.
	`create_housing_assignment` is the same write under the name and the argument
	spellings the handset actually posts, and the two must not come to hold two
	copies of the capacity rule. This wrapper still passes the flag as true on the
	caller's behalf and still declares no argument for it; that one is the choice
	that differs between them, so it is the one parameter the shared function
	takes.

	v0.63.1 ACCEPTS `unit` AND `assigned_date` HERE TOO. They are the handset's
	spellings of the cabin and the date, and until now a body carrying them
	arrived at this method with both dropped by `routes.bind` and was refused for
	want of a start date it had been sent — the same failure v0.62.0 fixed in the
	other direction, and the reason the refusal was hard to read is that it named
	an argument the caller had never heard of. Either spelling now decides, the
	refusals quote the one the body actually used, and two spellings carrying
	DIFFERENT cabins or dates is refused rather than resolved: one of them is a
	cabin somebody is not being put in and nothing in the body says which.

	WHAT IS STILL NOT ACCEPTED HERE IS `company` AND `allow_multi_occupancy`, and
	neither is an oversight. This method's promise is that it passes the barracks
	flag on the caller's behalf under capacity and refuses AT it — see above —
	and a spelling alias is not the place to hand a phone the one argument that
	changes that answer. `create_housing_assignment` is the door that declares
	both, and it is the one the hiring wizard posts to.
	"""
	unit_value, unit_label = _one_spelling(housing_unit, unit, "housing_unit", "unit")
	date_value, date_label = _one_spelling(check_in_date, assigned_date, "check_in_date", "assigned_date")
	return _house_one_person(
		user,
		employee=employee,
		unit=unit_value,
		assigned_date=date_value,
		end_date=end_date,
		company=None,
		deposit_paid=deposit_paid,
		housing_deduction_from_wages=housing_deduction_from_wages,
		notes=notes,
		allow_multi_occupancy=True,
		unit_label=unit_label,
		date_label=date_label,
	)


def _house_one_person(
	user: str,
	employee,
	unit,
	assigned_date,
	end_date,
	company,
	deposit_paid,
	housing_deduction_from_wages,
	notes,
	allow_multi_occupancy: bool,
	unit_label: str,
	date_label: str,
) -> dict:
	"""The housing write both wrappers make, with the one difference as an argument.

	v0.62.0. `assign_housing` (v0.54.0) and `create_housing_assignment` are the
	same act under two names — see this module's header on why the older spelling
	keeps its route — and every rule below is one an operator can defend in a wage
	claim or a Section 119 audit. Two copies of it would be two sets of camp rules
	to keep in step, which is the mistake the dispatch wrappers refuse to make with
	the concurrent-claim limit.

	`unit_label` AND `date_label` NAME THE ARGUMENT THE CALLER ACTUALLY SENT, so a
	refusal quotes the spelling that is on the handset's screen rather than the
	other wrapper's. A phone told "check_in_date is required" by a method it called
	with `assigned_date` is a phone whose operator cannot act on the sentence.

	THE CAPACITY CEILING IS NOT `allow_multi_occupancy`'s TO LIFT, whichever
	wrapper is calling. The flag says "this unit really is shared"; the count says
	how many beds are in it, and no flag on a phone adds one. See `assign_housing`
	on why the tool warns where this refuses.

	v0.94.0: `require_hiring_role`, NOT `require_hr_role` — ONE HELPER BEHIND BOTH
	`assign_housing` AND `create_housing_assignment`, so one line moves both routes
	and they cannot drift into two answers.

	WHAT REPLACES THE ROLE CHECK IS EVERY RULE BELOW IT, AND IT IS STRONGER THAN
	THE ROLE CHECK WAS. The overlap refusal, Oregon lawful occupancy, the capacity
	ceiling, the shower-block rule, the condemned-unit refusal and end-before-start
	all run whoever is calling — a foreman cannot overfill a cabin, and could not
	before. v0.93.0 deliberately routed the onboarding `housing_unit` argument
	through this helper so those refusals would be one implementation; that work
	was done and the role gate was the only thing left standing in front of it.
	"""
	allowed = guard.require_scope(user)
	personnel.require_hiring_role()
	compat.require_doctype(
		"Housing Assignment",
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)

	wanted = guard.require_company(user, company, allowed)
	person = guard.require_scoped_doc(EMPLOYEE, employee, "employee", allowed)
	unit = guard.require_docname(HOUSING_UNIT, unit, unit_label)
	start = str(assigned_date or "").strip()
	if not start:
		frappe.throw(
			f"{date_label} is required — an assignment with no start date is not a record.",
			frappe.ValidationError,
		)
	finish = str(end_date or "").strip()

	# The unit is scoped by its OWNING ENTITY, which is what a Housing Unit calls
	# its company. `require_scoped_doc` reads a field named `company` and there is
	# not one, so the check is made here rather than skipped — a cabin belonging
	# to an entity this caller cannot reach is not found, the same refusal as a
	# docname that does not exist.
	unit_row = (
		frappe.db.get_value(HOUSING_UNIT, unit, ["owning_entity", "capacity", "unit_name"], as_dict=True)
		or {}
	)
	owner = str(unit_row.get("owning_entity") or "")
	if owner and owner not in set(allowed):
		frappe.throw(f"{unit_label} {unit} was not found.", frappe.DoesNotExistError)
	# A `company` argument NARROWS, and a cabin outside it reads as not found for
	# the same reason one outside the caller's entities does. `require_company`
	# above has already refused a company this account cannot reach at all, so
	# what is left here is a real entity of theirs that this unit does not belong
	# to — which is a mis-tapped camp on the previous screen, not a permission
	# failure, and either way not somewhere this person is being put.
	if wanted and owner and owner != wanted:
		frappe.throw(f"{unit_label} {unit} was not found.", frappe.DoesNotExistError)

	capacity = int(unit_row.get("capacity") or 0)
	occupied = housing_tools.occupancy_for(unit, start, finish or None)
	if capacity and len(occupied) >= capacity:
		frappe.throw(
			f"{unit} holds {capacity} and already has {len(occupied)} assigned over these dates. "
			"Pick another unit, or end an assignment that has actually ended — "
			"list_available_housing shows what has a bed free. Nothing was created.",
			frappe.ValidationError,
		)

	inner = {
		"unit": unit,
		"employee": person,
		"assigned_date": start,
		"allow_multi_occupancy": bool(allow_multi_occupancy),
	}
	for key, value in (
		("end_date", finish),
		("deposit_paid", deposit_paid),
		("housing_deduction_from_wages", housing_deduction_from_wages),
		("notes", notes),
	):
		if value not in (None, ""):
			inner[key] = value

	data = housing_tools.create_housing_assignment(inner).data
	occupants_after = int(data.get("occupants_after") or 0)
	return {
		"assignment": data.get("name"),
		# v0.62.0. THE SAME DOCNAME UNDER THE KEY THE HANDSET READS. `assignment`
		# is what v0.54.0 called it and what anything already in the field is
		# parsing; `HousingAssignmentResult` decodes `name`, which is also what
		# every other write on this surface hands its docname back under. Both,
		# rather than a rename that would go quiet on a phone in an orchard.
		"name": data.get("name"),
		"employee": data.get("employee"),
		"employee_name": data.get("employee_name"),
		"unit": data.get("unit"),
		"unit_name": data.get("unit_name") or unit_row.get("unit_name") or data.get("unit"),
		"parcel": data.get("parcel"),
		"company": owner or None,
		"check_in_date": data.get("assigned_date"),
		# The handset's spelling of the same date, for the same reason `name` is
		# above: `OnboardingHousing.apiParams` sends `assigned_date` and
		# `HousingAssignmentResult` reads it back.
		"assigned_date": data.get("assigned_date"),
		"end_date": data.get("end_date"),
		"status": data.get("status"),
		"unit_capacity": data.get("unit_capacity"),
		"current_occupants": occupants_after,
		"occupied": occupants_after,
		"open_beds": max(0, capacity - occupants_after) if capacity else None,
		"housing_deduction_from_wages": data.get("housing_deduction_from_wages"),
		"deposit_paid": data.get("deposit_paid"),
		"warnings": data.get("warnings") or [],
		"section_119_note": data.get("section_119_note"),
	}


# ── 46. collect_signature ───────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("collect_signature", mutating=True, limit=guard.UPLOAD_LIMIT)
def collect_signature(
	user: str,
	doctype=None,
	docname=None,
	field=None,
	signature_base64=None,
	file_token=None,
	row=None,
	task=None,
	overwrite=None,
) -> dict:
	"""Attach a signature capture to the I-9 or W-4 box a task said was missing.

	THE CALL THE HIRING BOARD'S SIGNATURE TASKS EXIST FOR. A Farm Task raised by
	`i9_section_2_unsigned` carries `subject_doctype` and `subject_docname` —
	which form needs signing — and the app opens a signature pad over them. This
	is what the pad calls when the finger lifts.

	IT TAKES BYTES, WHICH `upload_signed_i9` DOES NOT, and the two are answering
	different questions rather than disagreeing. That one files a PHOTOGRAPH OF A
	SIGNED PAGE: megabytes, taken on a camera, and it goes up through
	`stage_file_chunk` / `finalize_staged_file` because a link that drops halfway
	through eight megabytes has to be resumable. This one carries what a finger
	drew on the glass: a few kilobytes of monochrome PNG, complete in one
	gesture, and chunking it would be three round trips to move less data than
	the JSON around it — three more places for a signature to be lost while the
	person who drew it walks back to the block. `tools/signatures.py` holds the
	512 KB ceiling that keeps the two apart, and something over it is told which
	door to use. `file_token` is still accepted for a caller that has one.

	A HIRING ROLE HOLDS THE PAD; THE ROSTER DECIDES WHOSE NAME GOES IN THE BOX.
	v0.94.0, and the change is which question this gate is asking. `require_hr_role`
	here was answering "who may attest FOR the employer" with a check on who may
	hold the phone, and `tools/signatures.py` — the module this delegates to —
	spends three paragraphs explaining that those are not the same question:
	requiring the account at the pad to be an authorized signer "would mean the
	only people who could collect a worker's signature are the people authorised
	to sign FOR the employer, which is precisely the conflation §274a keeps
	apart" (signatures.py `_require_signer`).

	SO THE GATE WAS BOTH REDUNDANT AND HARMFUL. Redundant on the employer boxes,
	because `SIGNATURE_BOXES` carries `signer_role="employer"` on I-9 Section 2
	and Supplement B and `_require_signer` runs the roster check on exactly those
	— Section 2 was already protected by the right mechanism, per person rather
	than per title. Harmful on the employee boxes, because Section 1 and the W-4
	employee signature are the WORKER'S OWN attestation, on nobody's roster and
	required to be on nobody's roster — so demanding an HR account to hold the pad
	meant a foreman could not collect a new hire's mark at all, and the hiring
	flow died here at step 4 with the two federal forms already raised.

	WHAT DID NOT MOVE: the per-box roster, the closed list of signable boxes,
	`_evidence_role`'s refusal of a mislabelled capacity, and the destroyed-I-9
	refusal. A foreman NOT on the roster still cannot sign Section 2 — the
	refusal simply names the roster now instead of naming a role.

	EVERY OTHER REFUSAL IS THE TOOL'S and is not copied here — the closed list of
	signable boxes, the authorized-signer roster on the two EMPLOYER boxes, the
	refusal to overwrite an attestation, the destroyed I-9. A second copy of
	those would be a second set of federal-form rules to keep in step.

	`worker_id` IS NOT AN ARGUMENT. Which task gets closed is worked out from the
	form and the alert type, and closing it goes through `complete_farm_task`,
	which refuses a completion from an account that is not holding the task. An
	account that could name somebody else here would be closing another person's
	task in their name.
	"""
	guard.require_scope(user)
	personnel.require_hiring_role()

	if not str(doctype or "").strip():
		frappe.throw(
			"doctype is required — 'I-9 Form' or 'W-4 Form'. The task you opened this from "
			"carries it in subject_doctype.",
			frappe.ValidationError,
		)
	if not str(docname or "").strip():
		frappe.throw(
			"docname is required — the form being signed. The task carries it in subject_docname.",
			frappe.ValidationError,
		)
	if not (str(signature_base64 or "").strip() or str(file_token or "").strip()):
		frappe.throw(
			"the signature image is required: signature_base64 for a capture taken on the "
			"pad, or file_token for one already uploaded.",
			frappe.ValidationError,
		)

	inner = {"doctype": doctype, "name": docname}
	for key, value in (
		("field", field),
		("signature_base64", signature_base64),
		("file_token", file_token),
		("row", row),
		("task", task),
		("overwrite", overwrite),
	):
		if value not in (None, ""):
			inner[key] = value

	data = signatures.collect_form_signature(inner).data
	return {
		"doctype": data.get("doctype"),
		"name": data.get("name"),
		"field": data.get("field"),
		"label": data.get("label"),
		"row": data.get("row"),
		"employee": data.get("employee"),
		"employee_name": data.get("employee_name"),
		"signature": data.get("signature"),
		"signed_at": data.get("signed_at"),
		"replaced": data.get("replaced"),
		# BOTH REPORTED, NEITHER FATAL. The app shows a completed signature and,
		# where the task did not close, why — usually because somebody else is
		# holding it, which is a thing the person at the pad needs to be told
		# rather than a failure of what they just did.
		"task": (data.get("task") or {}).get("task"),
		"task_completed": bool((data.get("task") or {}).get("completed")),
		"task_note": (data.get("task") or {}).get("note"),
		"pdf_regenerated": bool((data.get("pdf") or {}).get("regenerated")),
	}


# ── 47. submit_form_signature ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("submit_form_signature", mutating=True, limit=guard.UPLOAD_LIMIT)
def submit_form_signature(
	user: str,
	doctype=None,
	docname=None,
	signature_field=None,
	signature_image=None,
	signer_role=None,
	printed_name=None,
	employee=None,
	task=None,
	task_assignment=None,
	row=None,
	include_pdf=None,
	signer_badge=None,
	verification_method=None,
	device_id=None,
	gps_lat=None,
	gps_lon=None,
) -> dict:
	"""The signature pad's own call, in the shape `API_CONTRACT.md` §14.2 posts.

	THE SAME WRITE AS `collect_signature`, WITH THE APP'S OWN ARGUMENT NAMES AND
	THE APP'S OWN ANSWER. v0.55.0 published this work as `collect_signature`,
	which takes `field` and `signature_base64`; the client was written against
	§14.2, which sends `signature_field` and `signature_image` — and since
	`farmops_api/routes.bind` reduces a body to the keys the signature declares,
	a pad posting the contract's spelling at the old method lost the field name
	and the image on the way in and was told the signature was missing. Both
	methods stay: an older handset keeps the route it knows, and neither
	signature grows a second spelling of an argument.

	WHAT IS NEW BESIDES THE NAMES IS THE ANSWER, and it is what makes the
	compliance calendar a place work can be finished rather than read:

	  * `form_status` — what the form says now, so the screen can report "the
	    form now reads Complete" instead of "done";
	  * `dismissed_alert` — the alert this signature ANSWERED. Not a claim that
	    anything was dismissed: nothing here dismisses an alert, the sweep does
	    that by looking at the record again and finding the box filled. What the
	    phone needs is the name of the row it should take off the tab it was
	    tapped from, and only the server knows which row that is;
	  * `already_signed` — see below. It is the difference between a retry and a
	    second signature.

	IT IS IDEMPOTENT, WHICH §14.4 ASKS FOR BY NAME. A submission whose answer
	never made it back over a marginal link gets retried, and a worker who has
	already signed being shown an error is a worker who signs again — so a box
	that already carries an attestation answers success with
	`already_signed: true` and NOTHING IS OVERWRITTEN. Replacing an attestation
	somebody made under penalty of perjury is a deliberate act with an
	`overwrite` flag on it, and that flag is not reachable from here.

	`task` TRAVELS AND CLOSES THE WORK IN THE SAME TRANSACTION. The alert the
	phone tapped carries the Farm Task the sweep raised, and routing around the
	task list must not route around closing the task. `task_assignment` is
	accepted for the reason §6's is — one task can carry several assignments and
	closing it without naming the unit of work leaves the wrong one open — and is
	forwarded to the same completion path, which still refuses a completion filed
	by somebody who was not holding the work. That refusal is REPORTED rather
	than fatal: the signature is the compliance artefact and the task is
	bookkeeping about it.

	`signed_on` AND `image_format` ARE DROPPED, and by the documented mechanism
	rather than by accident — `routes.bind` keeps only the keys this signature
	declares, exactly as it drops `pdf_source`. The timestamp is the interesting
	one: §14.2 stamps it when the pad opened, and the column beside the image is
	the 8 CFR § 274a.2(h) record of when the attestation was made. A handset that
	could set it could backdate it, so the server stamps its own and answers with
	what it wrote — every key here is optional to the client, which reads the
	server's word for the record.

	`gps_lat` AND `gps_lon` USED TO BE DROPPED WITH THEM, AND ARE NOT ANY MORE.
	v0.60.0 — the reason they were dropped was never that a location is worth
	nothing; it was that the server had nowhere to put one, so a signature
	declaring them would have been a signature accepting data it discarded. The
	Signing Evidence register is that somewhere, and `signer_badge`,
	`verification_method` and `device_id` arrive with them: the badge is the
	IDENTITY step, resolved on the server against this employer's own register and
	REFUSED where it names somebody other than the worker whose form is open. The
	rest is corroboration and is recorded as such — a device UUID and a pair of
	coordinates are what the handset says about itself, and this app does not
	treat an unverifiable claim as a verified one.

	`include_pdf` HANDS BACK THE PAGE THAT WAS JUST SIGNED, AND DEFAULTS ON.
	v0.57.1. The signed form is the artefact the whole flow exists to produce and
	the person who drew the signature could not see it: `file_url` names a
	private File, and this app authenticates to THIS door with `X-FarmOps-Token`
	rather than to Frappe, so a private URL is a login page to it — the same
	reason `get_employee_badge_pass` puts its `.pkpass` in the answer. So the
	PDF travels as base64 beside the URL, and `render_i9_pdf` stamps the capture
	into the page content and flattens it, which means what comes back is the
	page WITH the signature on it rather than the blank-box copy.

	IT RENDERS ONE WHERE NONE EXISTED, which `collect_form_signature` will not do
	on its own — see `_redraw`, which argues at length that drawing a federal
	form nobody asked for is this app deciding something that is not its to
	decide. A caller passing this HAS asked, by name, in the same call. Turning
	it off is supported for a client that only wants the write.

	IT SEALS THE PAGE IT HANDS BACK, WHICH IS v0.63.0 AND STEP 5 OF THE CHAIN.
	Everything above collects the four things that happen in the field; the fifth
	is the server's, and until this release nothing took it. `seal` in the answer
	names the tamper-evident copy: the form with the captures stamped into the
	page content and the AcroForm flattened away, a verification page appended
	stating who signed, how they were identified, when, on what device, at what
	coordinates and what the record fingerprinted to, and a SHA-256 of the
	finished file filed back onto every Signing Evidence row it describes. It
	FOLLOWS `include_pdf`, because a caller that turned the page off wanted only
	the write, and sealing produces a page.

	NOT FATAL, EVER. The renderers need `pypdf` and the blank federal form on
	disk; a site missing either gets `pdf.available: false` with the reason in
	`pdf.note`, `seal.sealed: false` with the reason in `seal.note`, and a
	signature that is on the record regardless. A page is worth less than the
	attestation it depicts, and a call that threw away the second to avoid
	reporting the loss of the first would have the trade backwards.

	A HIRING ROLE HOLDS THE PAD; THE ROSTER DECIDES WHOSE NAME GOES IN THE BOX.
	v0.94.0, and the change is which question this gate is asking. `require_hr_role`
	here was answering "who may attest FOR the employer" with a check on who may
	hold the phone, and `tools/signatures.py` — the module this delegates to —
	spends three paragraphs explaining that those are not the same question:
	requiring the account at the pad to be an authorized signer "would mean the
	only people who could collect a worker's signature are the people authorised
	to sign FOR the employer, which is precisely the conflation §274a keeps
	apart" (signatures.py `_require_signer`).

	SO THE GATE WAS BOTH REDUNDANT AND HARMFUL. Redundant on the employer boxes,
	because `SIGNATURE_BOXES` carries `signer_role="employer"` on I-9 Section 2
	and Supplement B and `_require_signer` runs the roster check on exactly those
	— Section 2 was already protected by the right mechanism, per person rather
	than per title. Harmful on the employee boxes, because Section 1 and the W-4
	employee signature are the WORKER'S OWN attestation, on nobody's roster and
	required to be on nobody's roster — so demanding an HR account to hold the pad
	meant a foreman could not collect a new hire's mark at all, and the hiring
	flow died here at step 4 with the two federal forms already raised.

	WHAT DID NOT MOVE: the per-box roster, the closed list of signable boxes,
	`_evidence_role`'s refusal of a mislabelled capacity, and the destroyed-I-9
	refusal. A foreman NOT on the roster still cannot sign Section 2 — the
	refusal simply names the roster now instead of naming a role.

	EVERY OTHER REFUSAL IS STILL THE TOOL'S and is still not copied here — the
	closed list of signable boxes, the roster on the two employer boxes, the
	destroyed I-9. That was already the design; this release stops the wrapper
	contradicting it.
	"""
	guard.require_scope(user)
	personnel.require_hiring_role()

	if not str(doctype or "").strip():
		frappe.throw(
			"doctype is required — the form the signature goes on, e.g. 'I-9 Form'. The alert or "
			"task you opened the pad from carries it in signature_request.doctype.",
			frappe.ValidationError,
		)
	if not str(docname or "").strip():
		frappe.throw(
			"docname is required — the record being signed. It is signature_request.docname on "
			"the alert or task the pad was opened from.",
			frappe.ValidationError,
		)
	if not str(signature_image or "").strip():
		frappe.throw(
			"signature_image is required: the capture as bare base64, no data: preamble.",
			frappe.ValidationError,
		)

	wants_pdf = _as_flag(include_pdf, default=True)
	wants_seal = wants_pdf
	inner = {
		"doctype": doctype,
		"name": docname,
		"signature_base64": signature_image,
		"render_pdf": wants_pdf,
	}
	for key, value in (
		("field", signature_field),
		("row", row),
		("task", task),
		# v0.60.0. The evidence half. `signer_role` travels too and is CHECKED
		# rather than believed — `signatures._evidence_role` refuses a capacity the
		# box contradicts, so a pad that opened Section 1 and posted "employer"
		# gets a refusal instead of a mislabelled attestation.
		("signer_role", signer_role),
		("signer_badge", signer_badge),
		("verification_method", verification_method),
		("device_id", device_id),
		("gps_latitude", gps_lat),
		("gps_longitude", gps_lon),
	):
		if value not in (None, ""):
			inner[key] = value

	try:
		data = signatures.collect_form_signature(inner).data
	except signatures.AlreadySignedError:
		return _already_signed(doctype, docname, signature_field, task, wants_pdf)

	closed = data.get("task") or {}
	return {
		"doctype": data.get("doctype"),
		"docname": data.get("name"),
		# `field` RATHER THAN `signature_field` ON THE WAY BACK, because that is
		# what §14.3 answers with. The request and the response spell it
		# differently in the contract and both spellings are the app's.
		"field": data.get("field"),
		"form_status": _form_status(data.get("doctype"), data.get("name")),
		# v0.64.2. Null on every signature except the one that fills the last
		# outstanding attestation on a form whose Section 2 is already filed —
		# which is the moment the wizard's last step is waiting for. `form_status`
		# above says what the form reads NOW; this says whether THIS signature is
		# what moved it, and a screen that wants to announce "the I-9 is complete"
		# needs the second question rather than the first.
		"form_status_advanced_to": data.get("form_status_advanced_to"),
		"file_url": data.get("signature"),
		"task": closed.get("task"),
		"task_state": _task_state(closed.get("task")),
		"task_completed": bool(closed.get("completed")),
		"task_note": closed.get("note"),
		"signed_on": data.get("signed_at"),
		"already_signed": False,
		# v0.64.1. THE TOOL'S OWN READING WINS, and it has to. `collect_form_
		# signature` now re-runs the rules this box fires — so by the time this
		# projection runs, the alert has usually been dismissed and a fresh
		# lookup would answer with nothing on exactly the calls that worked. The
		# tool captured it before it swept; `_alert_answered` stays as the
		# fallback for the already-signed branch below, which sweeps nothing.
		"dismissed_alert": data.get("answered_alert")
		or _alert_answered(data.get("doctype"), data.get("field"), data.get("name")),
		"employee": data.get("employee"),
		"employee_name": data.get("employee_name"),
		# v0.60.0. The evidence row this signature produced, and — where it could
		# not be written, or was written without an identity check — the sentence
		# saying so. REPORTED RATHER THAN SILENT for the reason `task_note` is:
		# the person at the pad has done everything asked of them either way, and
		# an operation that is quietly collecting the weaker kind of evidence
		# should be able to find that out from the answer rather than from an
		# auditor.
		"evidence": (data.get("evidence") or {}).get("evidence"),
		"evidence_status": (data.get("evidence") or {}).get("status"),
		"evidence_note": (data.get("evidence") or {}).get("note") or None,
		# See the docstring. The page carries the capture stamped in, and the
		# bytes travel because a private File is a login page to this caller.
		"pdf": _signed_pdf(data.get("pdf") or {}) if wants_pdf else None,
		# v0.63.0. Step 5, taken automatically and reported honestly. See `_seal`.
		"seal": _seal(data.get("doctype"), data.get("name"), wants_seal),
	}


def _seal(doctype, docname, wanted: bool) -> dict:
	"""Seal the form the signature just landed on. NEVER RAISES, ALWAYS A DICT.

	THE LAST STEP IN THE ORDERING `tools/signatures.py` OPENS WITH, and it inherits
	that ordering's rule rather than getting its own: store the image, write it
	onto the form, record the evidence, close the task, redraw the PDF, seal it —
	and each step may fail without undoing the one before it, because THE
	SIGNATURE IS THE IRREPLACEABLE ARTEFACT and the person who drew it has gone
	back to work. A signature refused to keep the seal chain tidy would throw away
	the only thing that cannot be recovered.

	SO EVERY FAILURE IS REPORTED AND NONE IS FATAL. `sealed: false` with the reason
	in `note` is what a bench missing reportlab gets, and what a form whose page
	could not be redrawn gets, and the signature is on the federal record in both
	cases. An operator who finds `sealed: false` in an answer has been told, which
	is the difference between best-effort and silent — the same promise
	`evidence_status` makes one key above it.

	IT FOLLOWS `include_pdf`, which is the honest coupling rather than a shortcut:
	a caller that turned the page off is a caller that only wanted the write, and
	sealing produces a page. Turning it back on for them would be this method
	deciding it knows better.
	"""
	if not wanted:
		return {"sealed": False, "note": "include_pdf was off, so no sealed copy was produced."}
	if not (str(doctype or "").strip() and str(docname or "").strip()):
		return {"sealed": False, "note": "the signature's own document could not be identified."}
	try:
		data = signed_documents.seal_signed_document(
			{"document_type": doctype, "document_name": docname}
		).data
	except Exception as exc:
		return {
			"sealed": False,
			"note": (
				f"the signature is on the record and no sealed copy was produced ({exc}). The "
				f"attestation, its moment and its evidence row are unaffected; seal_signed_document "
				f"produces one later without collecting anything again."
			),
		}
	return {
		"sealed": bool(data.get("sealed")),
		"file_url": data.get("file_url"),
		"file_name": data.get("file_name"),
		"bytes": data.get("bytes"),
		"sealed_pdf_hash": data.get("sealed_pdf_hash"),
		"signatures_on_page": data.get("signatures_on_page"),
		"evidence_updated": data.get("evidence_updated") or [],
		# v0.64.1. Whether the sealed copy also reached the worker's personnel
		# folder. PROJECTED RATHER THAN DROPPED because it is the answer to the
		# question the gap was found by asking — "where is the completed I-9" —
		# and a handset that filed one has just put it somewhere an inspection
		# looks. `filed: false` carries the reason, exactly as `sealed` does.
		"employee_copy": data.get("employee_copy") or {"filed": False},
		"note": data.get("note") or None,
	}


def _signed_pdf(redrawn: dict) -> dict:
	"""The rendered page as something a handset can open, or why it has none.

	ALWAYS A DICT AND NEVER A RAISE. `_redraw` has already swallowed whatever the
	renderer did and reported it in `note`; this reads the File it named and can
	fail on its own — a File row written in a transaction that has not committed,
	a site whose private files directory moved. Both end the same way: the
	signature is on the record, and the phone is told there is no page rather
	than shown a failure for a write that succeeded.

	`available` IS THE KEY TO BRANCH ON, not the presence of `base64`. A page
	that rendered and could not be read back is a different problem from a site
	with no `pypdf`, and both are `available: false` with the reason in `note`.
	"""
	out = {
		"available": False,
		"regenerated": bool(redrawn.get("regenerated")),
		"file_url": redrawn.get("file_url"),
		"file_name": redrawn.get("file_name"),
		"content_type": "application/pdf",
		"base64": None,
		"bytes": None,
		"replaced": redrawn.get("replaced"),
		"note": redrawn.get("note"),
	}
	url = str(redrawn.get("file_url") or "").strip()
	if not url:
		return out
	out["file_name"] = out["file_name"] or url.rsplit("/", 1)[-1] or None
	try:
		docname = str(frappe.db.get_value("File", {"file_url": url}, "name") or "")
		content = file_tools.read_file_bytes(docname) if docname else b""
	except Exception as exc:  # pragma: no cover - see the docstring
		out["note"] = f"the page was rendered at {url} and could not be read back ({exc})."
		return out
	if not content:
		out["note"] = f"the page at {url} read back empty."
		return out
	out.update(
		{
			"available": True,
			"base64": base64.b64encode(content).decode("ascii"),
			"bytes": len(content),
		}
	)
	return out


def _as_flag(value, default: bool) -> bool:
	"""One optional boolean off a JSON body, tolerating the four ways it arrives.

	A phone sends `true`; a form post sends `"true"`; an older client sends `1`;
	and an absent key means the default rather than false. `args.as_bool` reads
	from a dict and these are already bound parameters, so the same tolerance is
	spelled out here rather than round-tripped through one.
	"""
	if value is None or value == "":
		return default
	if isinstance(value, bool):
		return value
	return str(value).strip().lower() in ("1", "true", "yes", "on")


def _already_signed(doctype, docname, field, task, wants_pdf: bool = False) -> dict:
	"""The §14.3 answer for a box that was already signed. A SUCCESS, not a miss.

	Answers with what is on the record rather than with what this call would have
	written, because nothing was written. The task is reported in whatever state
	it is actually in and is NOT closed from here: if the first attempt landed it
	closed the task then, and if somebody signed this box in the Desk instead
	then the task is theirs to close from the account holding it.

	THE PDF IS READ AND NOT DRAWN, for exactly that reason. A retry whose first
	attempt landed wants the same page back — the worker is standing there and
	the point of the answer is to show them what they signed — but rendering one
	here would make the idempotent path write, which is the one thing this
	branch exists not to do. So it hands back whatever page is already on the
	record, and a form nobody has rendered reports no page rather than growing
	one on a retry.
	"""
	resolved = str(doctype or "").strip()
	name = str(docname or "").strip()
	return {
		"doctype": resolved,
		"docname": name,
		"field": str(field or "").strip() or None,
		"form_status": _form_status(resolved, name),
		"task": str(task or "").strip() or None,
		"task_state": _task_state(task),
		"task_completed": None,
		"already_signed": True,
		"dismissed_alert": _alert_answered(resolved, field, name),
		"pdf": _existing_pdf(resolved, name) if wants_pdf else None,
		# NO SEAL EITHER, AND FOR THE SAME REASON AS THE EVIDENCE ROW BELOW. This
		# branch exists not to write, and sealing writes — a new File on the
		# personnel record and a stamp on every evidence row for the form. On the
		# retry path, where this branch happens most, that would mean a marginal
		# link produced a fresh sealed copy per attempt. The one the attempt that
		# LANDED produced is already attached and already named on the rows.
		"seal": {
			"sealed": False,
			"note": (
				"nothing was signed on this call, so nothing was sealed. The sealed copy for the "
				"attempt that landed is attached to this document and named on its Signing "
				"Evidence rows."
			),
		},
		# NO EVIDENCE ROW, AND THE KEYS ARE HERE SAYING SO. This branch exists not
		# to write, and an evidence row for a signature that was not collected
		# would be the register asserting an identity check on a call that made
		# none — on the retry path, where it would happen most. The row belonging
		# to the attempt that DID land is already in the register.
		"evidence": None,
		"evidence_status": None,
		"evidence_note": (
			"nothing was signed on this call, so no evidence row was written. The one for the "
			"attempt that landed is in the Signing Evidence register against this document."
		),
		"note": (
			"This box already carried a signature and nothing was changed. An attestation is "
			"replaced deliberately or not at all."
		),
	}


def _existing_pdf(doctype, docname) -> dict:
	"""The page already on the record, read back. Never renders and never raises."""
	handler = signatures.FORM_HANDLERS.get(str(doctype or "").strip()) or {}
	field = handler.get("pdf_field")
	if not (field and docname):
		return _signed_pdf({})
	try:
		url = str(frappe.db.get_value(doctype, docname, field) or "").strip()
	except Exception:  # pragma: no cover - a site whose column is not migrated
		url = ""
	if not url:
		return _signed_pdf(
			{
				"note": (
					f"no page has been rendered for this form. {handler.get('renderer')} draws "
					f"one, with the signature stamped in."
				)
			}
		)
	return _signed_pdf({"regenerated": False, "file_url": url})


def _form_status(doctype, docname) -> str | None:
	"""What the form says about itself now. None where the doctype has no status."""
	name = str(docname or "").strip()
	resolved = str(doctype or "").strip()
	if not (name and resolved) or not compat.has_field(resolved, "status"):
		return None
	try:
		return str(frappe.db.get_value(resolved, name, "status") or "") or None
	except Exception:  # pragma: no cover - a record deleted between write and read
		return None


def _task_state(task) -> str | None:
	name = str(task or "").strip()
	if not name or not compat.doctype_exists(FARM_TASK):
		return None
	try:
		return str(frappe.db.get_value(FARM_TASK, name, "state") or "") or None
	except Exception:  # pragma: no cover
		return None


def _alert_answered(doctype, field, docname) -> str | None:
	"""The alert a filled box makes untrue, by key. Never raises — see the tool."""
	box = signatures.BOXES_BY_KEY.get(f"{str(doctype or '').strip()}.{str(field or '').strip()}")
	if box is None:
		# The field was resolved by the tool from a doctype with one box, or this
		# is the already-signed path where the client may have named none.
		candidates = [
			entry for entry in signatures.SIGNATURE_BOXES if entry.doctype == str(doctype or "").strip()
		]
		if len(candidates) != 1:
			return None
		box = candidates[0]
	return signatures.alert_answered_by(box, str(docname or "").strip()) or None


# ── 39. log_shift_break ───────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("log_shift_break", mutating=True, limit=guard.WRITE_LIMIT)
def log_shift_break(
	user: str,
	shift=None,
	break_kind=None,
	started_at=None,
	duration_minutes=None,
	applies_to=None,
	employee=None,
	description=None,
) -> dict:
	"""Start a break on a shift — rest, meal, cool-down, water or shade.

	`break_kind` IS NOT `event_type` AND THE TWO ARE NOT THE SAME FIELD.
	`break_kind` is the payroll classification (Paid Rest, Unpaid Meal, Cool-Down,
	Water Break, Shade Break); `event_type` is derived from it and is never taken
	from the body. A phone that could set event_type directly could write a Rest
	Period with no break_kind, which would log on the compliance timeline and
	reach nothing in payroll — exactly the gap this method exists to close.

	v0.96.0 ADDED WATER BREAK AND SHADE BREAK, and the release note is a failure
	report rather than a feature: the two kinds the heat rules are written about
	were the two this method refused. A handset sending `Water Break` got
	"break_kind must be one of Paid Rest, Unpaid Meal, Cool-Down", the app kept
	the break locally, and the break log — which under OAR 437-004-1131 IS the
	evidence that heat relief was provided — was silently not created for exactly
	the events an inspector opens it to find. See `shifts.BREAK_KINDS` for why
	they are three records and one cool-down clock.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_SHIFT, shift, "shift", allowed)

	inner = {"shift": name, "break_kind": break_kind}
	for key, value in (
		("started_at", started_at),
		("duration_minutes", duration_minutes),
		("applies_to", applies_to),
		("description", description),
	):
		if value is not None:
			inner[key] = value
	if employee is not None:
		inner["employee"] = _employee_argument(employee, allowed)

	result = shifts.log_shift_break(inner)
	return result.data


# ── 40. end_shift_break ───────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("end_shift_break", mutating=True, limit=guard.WRITE_LIMIT)
def end_shift_break(user: str, shift=None, event=None, ended_at=None) -> dict:
	"""End a running break — write the observed duration.

	`event` is the name of the compliance event row, returned by
	`log_shift_break` in its response. The phone keeps it from the log call and
	passes it back here — same pattern as a task assignment docname.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_SHIFT, shift, "shift", allowed)

	inner = {"shift": name, "event": event}
	if ended_at is not None:
		inner["ended_at"] = ended_at

	result = shifts.end_shift_break(inner)
	return result.data


# ── 41. get_break_policy ──────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_break_policy", limit=guard.READ_LIMIT)
def get_break_policy(user: str, company=None, work_state=None) -> dict:
	"""The break schedule the handset counts its break coach from.

	A policy with no approver is returned with approved: false and IS STILL
	RETURNED. Withholding the schedule until somebody signs it would mean no
	break coach at all in the first season, which is worse than a coach whose
	provenance is visible.
	"""
	inner = {}
	if company is not None:
		inner["company"] = company
	if work_state is not None:
		inner["work_state"] = work_state

	result = shifts.get_break_policy(inner)
	return result.data


# ── 41b. get_break_schedule ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_break_schedule", limit=guard.READ_LIMIT)
def get_break_schedule(user: str, shift=None, farm_shift=None, planned_hours=None, employee=None) -> dict:
	"""Every break this shift owes, and the clock time each one falls due.

	v0.98.0, ITEM 14. THE WHOLE VALUE IS THAT ONE MACHINE DOES THE ARITHMETIC.
	`BreakSchedule` on the handset computes its countdown from the shift's start
	against the farm's policy when `get_break_policy` answers and against the
	state statutory minimum when it does not — Oregon and Washington are encoded
	in Swift, adult and minor, with the citations in the source. That is honest
	and it is not synchronised: seven phones in an orchard each work out their
	own instants from their own idea of when the shift began, and they disagree
	by whatever the clocks and the last sync disagree by. `SERVER_CHANGES.md` §14
	states the requirement as every phone counting down to the same second, and
	the only version of that which works is one computation and seven readers.

	IT ANSWERS WITH INSTANTS, NOT DURATIONS. A phone that fetches the schedule an
	hour into the shift shows the same 10:05 as the one that fetched it at the
	tailgate — a duration would have to be re-based against a device clock, which
	is the drift this exists to remove.

	`farm_shift` IS ACCEPTED ALONGSIDE `shift`, which is v0.96.0's item 1 applied
	before it can bite again: `DispatchAPI` sends the first spelling and the
	shift methods here declare the second, and this transport's argument filter
	drops what a signature does not name — so a body carrying only `farm_shift`
	would have arrived with no shift in it and been refused for the argument it
	had actually sent.

	`planned_hours` IS AN OVERRIDE AND USUALLY ABSENT. An open shift is assumed
	to run `shifts.PLANNED_SHIFT_HOURS`, because the entitlement bands are keyed
	on how long the shift IS and not on how long it has been — computing against
	elapsed time would make the meal period appear four hours in, which is three
	hours after the crew needed to know it was coming. A closed shift is measured
	end to end and this argument does not enter into it.

	`employee` NAMES WHOSE SCHEDULE IT IS, and it is what makes the purple
	"Minor's schedule" badge true rather than decorative. A worker under
	eighteen is owed a rest every two hours and a meal every four (OAR
	839-021-0072), so their countdown is a DIFFERENT SET OF INSTANTS from the
	crew's — and the whole argument for computing this on the server is that
	seven phones count to the same second, which fails immediately if one of the
	seven is a minor whose schedule the server does not know about. Omitted, the
	answer is the crew's and `schedule_band` says `adult`.

	IT IS RUN THROUGH `_employee_argument` LIKE EVERY OTHER PERSON-NAMING KEY on
	this surface. A break schedule is not sensitive; being able to name anybody
	on the site is, and the same gate everywhere is cheaper than a judgement per
	endpoint about which reads deserve one.

	THE ROLE GATE IS THE SHIFT ROLE, INSIDE THE TOOL, and the scope is the
	caller's own entities: a break schedule names no worker, but it names a
	shift, and which crews are working today is not a fact for an account that
	cannot reach the entity running them.
	"""
	allowed = guard.require_scope(user)
	named, label = _one_spelling(shift, farm_shift, "shift", "farm_shift")
	docname = guard.require_scoped_doc(FARM_SHIFT, named, label, allowed)

	inner = {"shift": docname}
	if planned_hours is not None:
		# Unparsed: `as_float` inside the tool is what refuses "eight" in a
		# sentence, where a `float()` here would answer the same body with a 500.
		inner["planned_hours"] = planned_hours
	if employee is not None:
		inner["employee"] = _employee_argument(employee, allowed)
	return shifts.get_break_schedule(inner).data


# ── 42. clock_out_worker ──────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("clock_out_worker", mutating=True, limit=guard.WRITE_LIMIT)
def clock_out_worker(user: str, shift=None, employee=None, left_at=None, notes=None) -> dict:
	"""End one worker's time on a shift that continues without them.

	Named `clock_out_worker` on this surface rather than `remove_worker_from_shift`,
	because the phone's verb is the operational one and the tool's verb is the
	storage one.

	THE EMPLOYEE GUARD IS THE SAME ONE `add_worker_to_shift` USES. An account that
	can name somebody else's employee — somebody from another entity entirely — is
	not scoped to anything, and scoping to the caller's own entities is the
	minimum that makes it safe.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_SHIFT, shift, "shift", allowed)
	person = _employee_argument(employee, allowed)

	inner = {"shift": name, "employee": person}
	if left_at is not None:
		inner["left_at"] = left_at
	if notes is not None:
		inner["notes"] = notes

	result = shifts.remove_worker_from_shift(inner)
	return result.data


# ── 43. get_shift_production ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_shift_production", limit=guard.READ_LIMIT)
def get_shift_production(user: str, shift=None) -> dict:
	"""Per-worker bucket counts for a shift, sorted by count desc.

	The production board. Polled on every successful bucket sync rather than on a
	timer, so a board that refreshes when something changed is both cheaper and
	fresher than one on a clock.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_SHIFT, shift, "shift", allowed)

	result = shifts.get_shift_production({"shift": name})
	return result.data


# ── 44. get_shift ─────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_shift", limit=guard.READ_LIMIT)
def get_shift(user: str, shift=None) -> dict:
	"""The shift with its crew, events, weather and break summary.

	This is the read the close screen renders and what the audit packet reads.
	Existing as an MCP tool since v0.19.3, and now reachable from a phone.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_SHIFT, shift, "shift", allowed)

	result = shifts.get_shift({"shift": name})
	return result.data


# ── 45. list_shifts ───────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_shifts", limit=guard.READ_LIMIT)
def list_shifts(user: str, company=None, status=None, mine=None, limit=None, timezone=None) -> dict:
	"""Which shifts are still open, so a phone can find one it lost.

	THE READ THAT CLOSED A HOLE IN THE HANDSET, AND THE HOLE WAS PERMANENT.
	`get_shift` and `end_shift` both take a docname. `start_shift` hands one back
	and the app held it in memory — so a dismissed screen, a tab switch, a
	relaunch or a flat battery lost the only copy, and the shift stayed open with
	nothing on the phone able to name it. There was no read on this surface that
	could find it again: this is that read. A shift that never closes is a crew
	with no Attendance rows for the day, because `end_shift` is what writes them.

	`mine` DEFAULTS TO TRUE AND THAT IS THE WHOLE POINT OF THE ARGUMENT. Closing
	a shift signs an FSMA §112.161(b) supervisor review in somebody's name, and a
	register handed to a phone unfiltered is a list of other foremen's shifts with
	a Close button next to each. The default answers "which of MINE is open"; a
	caller who genuinely wants the company's register — a manager clearing up
	after a foreman who has gone home — passes `mine=false` and is still held to
	`require_hr_role` and the company scope underneath. An account with no linked
	Employee has no shifts of its own, so `mine=true` answers nothing rather than
	falling back to everything.

	`status` is passed through to the tool, which COMPUTES Active/Closed from the
	absence of an end time rather than reading a stored column — so "Active" is
	the question a close actually changes.
	"""
	allowed = guard.require_scope(user)
	wanted = guard.require_company(user, company, allowed)
	only_mine = _as_flag(mine, True)

	inner = {}
	if wanted:
		inner["company"] = wanted
	if status not in (None, ""):
		inner["status"] = str(status).strip()
	if limit is not None:
		# Clamped inside the tool against its own RECORD_CAP — passed through
		# rather than second-guessed here.
		inner["limit"] = limit
	if timezone:
		inner["timezone"] = timezone

	if only_mine:
		person = fieldwork._employee_for(user)
		if not person:
			# No Employee behind this login, so there is no shift it could own.
			# ANSWERED RATHER THAN REFUSED, and not widened to the whole register
			# either: an office account reading its own open shifts and finding
			# none is a correct answer, and falling back to everybody's would
			# quietly turn `mine=true` into `mine=false` for exactly the accounts
			# least likely to notice.
			return {"shifts": [], "count": 0, "company": wanted or None, "mine": True}
		inner["foreman"] = person

	data = dict(shifts.list_shifts(inner).data)
	# Scoped again on the way out for the reason `list_my_tasks` does it: the
	# tool filters on the companies it was told about and `allowed` is what this
	# CREDENTIAL may see, which is the narrower of the two.
	data["shifts"] = guard.scoped(data.get("shifts") or [], allowed)
	data["count"] = len(data["shifts"])
	data["mine"] = only_mine
	return data


# ════════════════════════════════════════════════════════════════════════════
# v0.62.0 — THE SEVEN THE APP CALLS AND THIS SURFACE DID NOT ANSWER
# ════════════════════════════════════════════════════════════════════════════
#
# `MobileAPI.swift` was audited against v0.61.0 on 2026-08-12 and named seven
# paths that 404. Three of them are methods that exist under another spelling and
# four are methods that do not exist here at all. This block is both halves.
#
# THE THREE ALIASES ARE ALIASES AND NOT RENAMES, and that is the whole design of
# them. A rename would fix the handset in the next TestFlight build and break
# every phone already in an orchard on the release it shipped in — this surface's
# contract with a device in the field is that a method it can reach today it can
# still reach tomorrow, which is why `collect_signature` kept its route when
# `submit_form_signature` arrived at v0.57.0 with the argument spellings
# `API_CONTRACT.md` actually posts. Same act, two doors, ONE implementation
# behind them: each of the three delegates to a private function the older
# wrapper now also calls, so the camp rules, the capacity ceiling and the entity
# scoping cannot come to differ between the two names.
#
# AN ALIAS IS NOT A BARE FORWARD, BECAUSE `routes.bind` REDUCES A BODY TO THE
# KEYS A SIGNATURE DECLARES. Two of the three needed a parameter change to be
# correct rather than merely reachable, and this is what the iOS audit's own note
# is about: a rename alone would have turned a loud 404 into a quiet wrong
# answer. `list_housing_units` declares `assignable_only` where the older name
# declares `include_full`, and a dropped filter would have listed cabins nobody
# can be put in. `create_housing_assignment` declares `unit`, `assigned_date`,
# `company` and `allow_multi_occupancy` where the older name declares
# `housing_unit`, `check_in_date` and neither of the last two — so a rename would
# have arrived with no unit, no date, and the barracks flag silently gone.
#
# v0.63.1 DECLARES BOTH SPELLINGS AT BOTH DOORS, because the paragraph above
# describes a drop that has a mirror image. `bind` reducing a body to one
# signature's keys costs a caller the filter whichever direction they cross in:
# `include_full` sent at `list_housing_units` vanished exactly as `assignable_only`
# sent at `list_available_housing` did, and `housing_unit`/`check_in_date` sent at
# `create_housing_assignment` vanished exactly as `unit`/`assigned_date` did — so
# a client written against either name got a wrong list of beds or a refused hire
# the moment it reached the other. Each door still keeps its OWN default and its
# own barracks behaviour; what it no longer does is silently ignore the other
# door's word for the same thing. `_camp_breadth` and `_one_spelling` are where
# the pairs are reconciled, and a body that says both to contradictory effect is
# refused there by name rather than settled in the code's favour.
#
# THE BARRACKS FLAG IS FORWARDED HERE AND IS STILL NOT AN OVERRIDE. See
# `_house_one_person`: the capacity ceiling refuses before the flag is read, on
# both doors. What the flag decides is the case UNDER capacity — a bunk room that
# really is shared, said out loud, versus a foreman tapping the same cabin twice —
# and that is a question only the person standing there can answer.


# ── 45. list_org_reference_data ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_org_reference_data", limit=guard.READ_LIMIT)
def list_org_reference_data(user: str, company=None) -> dict:
	"""`list_onboarding_reference_data` under the name the handset calls it.

	v0.62.0. The four dropdowns on the wizard's Assignment step, and the one of
	the three aliases that needed no argument change at all: both spellings take
	`company` and nothing else. Every rule — the scoping, the absent masters, the
	branch-to-parcel mapping — is in `list_onboarding_reference_data`'s docstring
	and in the function both of them call.
	"""
	return _onboarding_reference_data(user, company)


# ── 46. list_housing_units ──────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_housing_units", limit=guard.READ_LIMIT)
def list_housing_units(
	user: str,
	company=None,
	parcel=None,
	branch=None,
	assignable_only=None,
	employee=None,
	include_full=None,
) -> dict:
	"""`list_available_housing` under the handset's name and its filter's spelling.

	v0.62.0. Every rule is `list_available_housing`'s and both names run the same
	function; what differs is one argument, and it differs in BOTH its name and
	its sense.

	`assignable_only` IS THE NEGATIVE OF `include_full`, AND THE DEFAULT FLIPS
	WITH IT. `HousingAPI.listUnits` sends the flag only when a caller asks for the
	open beds alone, so the ordinary call from the wizard asks for the WHOLE camp —
	the full cabins and the condemned one included, marked and greyed out, because
	a foreman who cannot find the cabin they expected needs to be told it is full
	rather than shown a shorter list. The older name defaults the other way, which
	is right for the question IT was written for ("where can somebody sleep") and
	is not the question this one is asked.

	A dropped filter is exactly the failure the iOS audit refused to risk with a
	bare rename: `routes.bind` keeps only the keys a signature declares, so
	`assignable_only` sent at a method that names `include_full` is not an error —
	it is a filter that vanishes, and the list comes back full of cabins nobody can
	be put in. Declaring it here is what makes that impossible.

	NON-RESIDENTIAL UNITS ARE STILL ABSENT ENTIRELY, under either name. A shower
	block is not a bed with a problem; it is not a bed. See `list_available_housing`.

	v0.63.1 ACCEPTS `include_full` HERE TOO, AND THE DEFAULT ABOVE IS UNMOVED. The
	older spelling is declared for the same reason this method declares the newer
	one: `routes.bind` drops what a signature does not name, so a caller who knew
	only `include_full` was getting this door's WIDE default whatever it sent —
	including when it sent `include_full=false` and meant the open beds alone.
	Both spellings now decide, in their own sense, and a body carrying neither
	still gets the whole camp. Sending both to the same effect is refused by name
	rather than resolved; see `_camp_breadth`.
	"""
	return _available_housing(
		user,
		company=company,
		parcel=parcel,
		branch=branch,
		# True: the handset sends its flag only to NARROW, so a body naming neither
		# spelling is the wide answer — the whole camp, the full cabins marked.
		include_full=_camp_breadth(include_full, assignable_only, default_full=True),
		employee=employee,
	)


# ── 47. create_housing_assignment ───────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_housing_assignment", mutating=True, limit=guard.WRITE_LIMIT)
def create_housing_assignment(
	user: str,
	employee=None,
	unit=None,
	assigned_date=None,
	end_date=None,
	company=None,
	deposit_paid=None,
	housing_deduction_from_wages=None,
	notes=None,
	allow_multi_occupancy=None,
	housing_unit=None,
	check_in_date=None,
) -> dict:
	"""`assign_housing` under the name and the four argument spellings the app posts.

	v0.62.0. The write is `_house_one_person`'s and so is every refusal in it: the
	HR role gate, the entity scoping on both the person and the cabin, the capacity
	ceiling this surface enforces where the tool only warns, and the tool's own
	rules about a shower block, a condemned unit and an end date before a start.

	FOUR ARGUMENTS DIFFER FROM `assign_housing` AND EACH ONE MATTERS:

	  * `unit` and `assigned_date` are what `OnboardingHousing.apiParams` sends.
	    The older wrapper declares `housing_unit` and `check_in_date`, and
	    `routes.bind` keeps only the keys a signature names — so this method under
	    the older signature would have received a body with no unit and no date in
	    it and refused every hire for want of a start date it was sent.
	  * `company` NARROWS THE CABIN, and the wizard sends it because it has just
	    hired somebody into that entity. A unit belonging to another of the
	    caller's entities reads as not found rather than as refused, which is the
	    rule every other docname on this surface follows.
	  * `allow_multi_occupancy` IS THE BARRACKS CASE AND IT IS OFF BY DEFAULT,
	    which is the opposite of what `assign_housing` does and is deliberate on
	    both sides. That method cannot receive the flag, so it passes true on the
	    caller's behalf; a bunk room and a double-tap look identical on the wire,
	    and the older wrapper resolved the ambiguity in favour of the bunk room.
	    The handset can answer the question properly — the foreman is standing
	    there — so here the default refuses the second body, NAMING who is already
	    in the cabin, and the flag is the deliberate second tap.

	THE FLAG DOES NOT LIFT THE CAPACITY CEILING and there is no argument that
	does. `_house_one_person` counts the beds before it writes and refuses a unit
	that is already at capacity whatever the body said, because nothing on a phone
	adds a bunk to a cabin and a bed that does not exist becomes somebody sleeping
	in a truck.

	v0.63.1 ACCEPTS `housing_unit` AND `check_in_date` HERE TOO — the older
	wrapper's spellings of the same cabin and the same date, declared so that a
	client written against `assign_housing` reaches this door with its body
	intact rather than with the two fields `routes.bind` would otherwise drop.
	Either spelling decides; the refusals quote the one the body actually used;
	two spellings naming different cabins or dates is refused rather than
	resolved. Nothing else about this method changes with them — `company` still
	narrows, and `allow_multi_occupancy` is still off by default, which is the
	one behaviour that differs between the two doors and the reason both exist.
	"""
	unit_value, unit_label = _one_spelling(unit, housing_unit, "unit", "housing_unit")
	date_value, date_label = _one_spelling(assigned_date, check_in_date, "assigned_date", "check_in_date")
	return _house_one_person(
		user,
		employee=employee,
		unit=unit_value,
		assigned_date=date_value,
		end_date=end_date,
		company=company,
		deposit_paid=deposit_paid,
		housing_deduction_from_wages=housing_deduction_from_wages,
		notes=notes,
		allow_multi_occupancy=_said_yes(allow_multi_occupancy),
		unit_label=unit_label,
		date_label=date_label,
	)


#: What `set_employee_org_fields` writes, in the order the wizard's Assignment
#: step reads them. Every one is on `tools/employee.WRITABLE`, which is what
#: makes this wrapper a subset rather than a second allowlist.
ORG_FIELDS = ("branch", "department", "designation", "employment_type", "date_of_joining")

#: What `set_employee_contact_fields` writes: the handset's spelling on the left,
#: this site's Employee column on the right.
#:
#: THE MAP EXISTS BECAUSE FRAPPE HR'S COLUMN NAMES ARE NOT WHAT ANYBODY CALLS
#: THESE FIELDS. `person_to_be_contacted` is labelled "Emergency Contact Name" on
#: the form itself, and a phone should not have to know the docname of a column
#: to file a phone number against it. `company` is not here and is not writable
#: through this method — which entity employs somebody is the Assignment step's
#: fact and `set_employee_org_fields`'s to change.
CONTACT_FIELDS = {
	"cell_phone": "cell_number",
	"personal_email": "personal_email",
	"current_address": "current_address",
	"emergency_contact_name": "person_to_be_contacted",
	"emergency_phone": "emergency_phone_number",
}


# ── 48. set_employee_org_fields ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("set_employee_org_fields", mutating=True, limit=guard.WRITE_LIMIT)
def set_employee_org_fields(
	user: str,
	employee=None,
	company=None,
	branch=None,
	department=None,
	designation=None,
	employment_type=None,
	date_of_joining=None,
) -> dict:
	"""Where one person is filed: branch, department, job title, class, start date.

	v0.62.0, AND THE STEP IT COMPLETES HAS HAD NOWHERE TO WRITE SINCE v0.54.0.
	`list_onboarding_reference_data` has served the four dropdowns for six
	releases and this surface published no method that could put the chosen values
	on the record — so the Assignment step read beautifully off the site, asked a
	foreman four questions, and dropped all four answers. A returning worker's
	record is never created at all, which is why this cannot be folded into
	`create_employee`: on the common path in tree fruit there is nothing to create.

	IT IS A SUBSET OF `update_employee` AND NOT A SECOND WRITER. Every field it
	takes is on `tools/employee.WRITABLE`, the tool runs its own HR role gate and
	its own company scoping, and the Link validation against THIS site's Branch,
	Department, Designation and Employment Type records is the tool's — which is
	the same delegation `create_employee` makes and for the same reason: a second
	copy of the personnel rules is a second set to keep in step.

	AN UNSENT FIELD IS LEFT ALONE AND AN EMPTY ONE IS NOT AN ANSWER. The step is
	shown to returning workers whose department was set in the office last season,
	and a call that wrote "" for every untouched picker would clear four columns
	somebody filled in deliberately. So a blank is dropped here rather than passed
	as a clear — a caller that genuinely means "remove this person's department"
	is asking for something a hiring wizard does not do, and does it in the Desk.

	WHAT CAME BACK IS WHAT STUCK. `skipped` names any field this site's Employee
	doctype does not carry — `branch` on a bench without Frappe HR's Branch master
	is the real case — because a step that assumed its own optimism would show a
	green tick over a department nobody has. It is `update_employee`'s
	`fields_not_on_this_site` under the name `AppliedOrgFields` decodes.

	THE HR ROLE IS THE TOOL'S GATE AND IT IS NOT COPIED HERE. See this module's
	header: only Farm Manager holds both a Farm Ops grant and an HR role, which is
	the enrolment an operator running the hiring wizard already needs.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee, allowed)
	# Validated but NOT forwarded. `company` scopes the caller — an account naming
	# an entity it cannot reach is refused here rather than at the tool — and
	# re-pointing an Employee at a different company is a transfer, not an
	# assignment step, so this method does not do it. `update_employee` will, from
	# the console, where somebody can mean it.
	guard.require_company(user, company, allowed)

	sent = {
		"branch": branch,
		"department": department,
		"designation": designation,
		"employment_type": employment_type,
		"date_of_joining": date_of_joining,
	}
	inner = {"name": person}
	for key in ORG_FIELDS:
		value = str(sent.get(key) or "").strip()
		if value:
			inner[key] = value

	if len(inner) == 1:
		frappe.throw(
			"Nothing was sent to write. Pass at least one of: " + ", ".join(ORG_FIELDS) + ".",
			frappe.ValidationError,
		)

	data = personnel.update_employee(inner).data
	# Read back off the record rather than echoed off the request. See the
	# docstring: what the step reports has to be what the row says, and a Link
	# this site cannot resolve leaves its column unset while the call succeeds.
	current = (
		frappe.db.get_value(
			EMPLOYEE, person, compat.existing_fields(EMPLOYEE, list(ORG_FIELDS)), as_dict=True
		)
		or {}
	)
	return {
		"employee": person,
		"employee_name": data.get("employee_name"),
		"company": data.get("company"),
		"branch": current.get("branch"),
		"department": current.get("department"),
		"designation": current.get("designation"),
		"employment_type": current.get("employment_type"),
		"date_of_joining": str(current.get("date_of_joining") or "") or None,
		"changed": data.get("changed") or [],
		"unchanged": data.get("unchanged") or [],
		"skipped": data.get("fields_not_on_this_site") or [],
	}


# ── 49. set_employee_contact_fields ─────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("set_employee_contact_fields", mutating=True, limit=guard.WRITE_LIMIT)
def set_employee_contact_fields(
	user: str,
	employee=None,
	cell_phone=None,
	personal_email=None,
	current_address=None,
	emergency_contact_name=None,
	emergency_phone=None,
) -> dict:
	"""How to reach one person, and who to ring if something happens to them.

	v0.62.0. A SEPARATE METHOD FROM `set_employee_org_fields` BECAUSE IT IS A
	DIFFERENT FACT, which is the handset's own argument and the right one: that
	one says where somebody is filed and this says how to reach them. A phone
	number is exactly the field somebody will want to correct on its own, from a
	screen that has nothing to do with which ranch a picker reports to.

	THE ARGUMENT NAMES ARE THE HANDSET'S AND THE COLUMNS ARE FRAPPE HR'S, mapped
	by `CONTACT_FIELDS`. `emergency_contact_name` is `person_to_be_contacted` on
	the doctype and `emergency_phone` is `emergency_phone_number`; the labels on
	the form are the words this method takes, because a phone should not have to
	know the docname of a column to file a number in it.

	THE LAST THREE ARE NEW TO `tools/employee.WRITABLE` AT v0.62.0 and that is a
	decision rather than a convenience — see that module's docstring, where the
	list is closed on purpose. An emergency contact is the same KIND of fact as
	the cell number beside it: how somebody is reached, and by whom, on the day it
	matters. An operation that cannot answer the second question at four in the
	afternoon in August has a real problem, and the answer is collected on the day
	somebody is hired or it is not collected at all. None of the three is payroll,
	tax or banking, which is the boundary that list actually defends.

	AN UNSENT FIELD IS LEFT ALONE AND AN EMPTY ONE IS NOT AN ANSWER — the same
	rule as the org write, and here it is load-bearing: the step opens on what is
	already filed for a returning worker, and a call that sent "" for an untouched
	box would erase the only way anybody had to reach them.

	IT IS A SUBSET OF `update_employee`, so the HR role gate, the company scoping
	and the schema check are all the tool's. `skipped` reports a column this site's
	Employee does not carry rather than failing the hire over it.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee, allowed)

	sent = {
		"cell_phone": cell_phone,
		"personal_email": personal_email,
		"current_address": current_address,
		"emergency_contact_name": emergency_contact_name,
		"emergency_phone": emergency_phone,
	}
	inner = {"name": person}
	for spoken, column in CONTACT_FIELDS.items():
		value = str(sent.get(spoken) or "").strip()
		if value:
			inner[column] = value

	if len(inner) == 1:
		frappe.throw(
			"Nothing was sent to write. Pass at least one of: " + ", ".join(CONTACT_FIELDS) + ".",
			frappe.ValidationError,
		)

	data = personnel.update_employee(inner).data
	current = (
		frappe.db.get_value(
			EMPLOYEE, person, compat.existing_fields(EMPLOYEE, list(CONTACT_FIELDS.values())), as_dict=True
		)
		or {}
	)
	absent = set(data.get("fields_not_on_this_site") or [])
	return {
		"employee": person,
		"employee_name": data.get("employee_name"),
		# Reported under the names that were SENT, not the columns they landed in.
		# A caller told that `person_to_be_contacted` was skipped has to work out
		# that it asked for `emergency_contact_name`, and the map that answers that
		# is on this side of the wire.
		**{spoken: current.get(column) for spoken, column in CONTACT_FIELDS.items()},
		"changed": data.get("changed") or [],
		"unchanged": data.get("unchanged") or [],
		"skipped": sorted(spoken for spoken, column in CONTACT_FIELDS.items() if column in absent),
	}


#: The parent doctypes whose attachments a phone may read, and whether reading
#: one is a personnel act.
#:
#: A CLOSED LIST, FOR THE REASON `attach_onboarding_document` NAMES ONE PARENT IN
#: CODE. `files.list_attachments` takes any doctype on the site, which is right on
#: an MCP console and is not right here: a field worker who could name the parent
#: could walk the File table one docname at a time — a lease, a bank statement, a
#: governance document — through a method whose whole job is to hand back what is
#: filed against it. The tool's own Frappe permission check would refuse most of
#: that; this refuses the question.
#:
#: THE FLAG IS WHETHER THE HR ROLE RIDES WITH IT. An Employee's folder and an
#: I-9's are the photographs of somebody's identity documents, which is the
#: personnel read `search_employees` and `assign_housing` both gate; a Farm Task's
#: evidence and a Housing Inspection's photographs are field work, and the six
#: gates `guard.endpoint` has already run are the whole of what those need.
#:
#: v0.92.2 ADDS `Farm Payroll Entry`, AND IT IS THE THIRD PERSONNEL PARENT. A pay
#: stub is attached to the RUN rather than to the person — one run carries one
#: file per crew member, which `_attached_stub_urls` matches by name — so the
#: folder behind this docname is a crew's wages and the flag has to be True.
#: `False` was the shape asked for and would have said, in this app's own voice,
#: that a foreman may open it; only Frappe's DocPerm on the doctype would have
#: been left refusing, and `routes.py` already argues that putting a crew's wages
#: in front of every foreman is the reflex to avoid.
#:
#: THE WORKER'S OWN STUB DOES NOT COME THROUGH HERE AT ALL. `get_my_pay_stub_pdf`
#: carries its bytes in its own answer, because the right a picker holds is not
#: "this run" but "the one file on it that is mine" — narrower than any doctype
#: flag can say, and narrower than Frappe's parent permission can express.
ATTACHMENT_PARENTS = {
	EMPLOYEE: True,
	"I-9 Form": True,
	"Farm Task": False,
	"Farm Task Assignment": False,
	HOUSING_UNIT: False,
	"Housing Inspection": False,
	"Compliance Alert": False,
	"Farm Shift": False,
	# S10, the other half. Filing a photograph against a report and never being
	# able to list it back is the gap `list_attachments` was added to close for
	# Employee in v0.62.0, and it would be reopened here by adding only the
	# write. `False` — NO HR GATE — because this app does not treat an Accident
	# Report as a personnel document anywhere else it decides: `NARRATIVE_TARGETS`
	# maps it to None beside `Farm Task` while flagging `Farm Incident Record`
	# "hr", and `get_accident_report` takes no role beyond enrolment.
	#
	# THE DOCTYPE'S OWN DOCPERMS ARE NARROWER (System Manager, HR Manager) and
	# `files.list_attachments` honours them, exactly as the `Farm Incident
	# Record` entry above says of itself. So this opens the door for the accounts
	# that hold those roles and manufactures a permission for nobody.
	ACCIDENT_REPORT: False,
	payroll_tools.PAYROLL_ENTRY: True,
	# v0.96.0. The photographs `create_discipline_record` now files against an
	# incident record, readable back. `True` — the HR gate — because this is a
	# personnel document in the same sense an I-9 is: REPORTING what happened is
	# the field role's since v0.94.0, and READING somebody's disciplinary file is
	# not, which is the line `get_discipline_record` already draws. Note the
	# doctype's own DocPerms are narrower still (System Manager, HR Manager), and
	# `files.list_attachments` honours them — so this entry opens the door for
	# the accounts that hold those roles and does not manufacture a permission
	# for anybody else.
	discipline_tools.DISCIPLINE: True,
	# v0.98.0. The SOP a standing job carries, readable by whoever is doing the
	# job. `False` — no HR gate — because a Farm Task Template is not a fact
	# about a person: it is what this farm does when it cleans a cabin, and the
	# picker holding the phone in front of the cabin is exactly who the document
	# was written for.
	#
	# IT IS THE ONLY PARENT ON THIS LIST THAT IS A DEFINITION RATHER THAN AN
	# EVENT. Everything else here is a record of something that happened to
	# somebody — a shift, an inspection, a warning, a payroll run — and is
	# scoped to the caller's entities by `_attachment_parent` on that basis. A
	# template belongs to the OPERATION rather than to an entity and may carry no
	# company at all, which `require_scoped_doc` reads as reachable, the same way
	# `list_farm_task_templates` and `create_task_from_template` already do.
	#
	# READ-ONLY IN PRACTICE AND NOT BY DECLARATION. Nothing on this surface
	# writes a template — `create_farm_task_template` and
	# `update_farm_task_template` are deliberately absent from `routes.py`,
	# which says why — so the folder behind this entry is filled at a desk and
	# read in an orchard, which is the whole shape of item 2.
	FARM_TASK_TEMPLATE: False,
}

#: The parents whose folder this surface opens on the strength of ITS OWN gates
#: rather than on Frappe's DocPerm for the doctype. ONE ENTRY, AND IT IS MEANT TO
#: STAY THAT SHORT.
#:
#: v0.100.1. WHAT WENT WRONG. A farm owner opening an employee's Documents
#: section on the handset got "…is not permitted to read Employee HR-EMP-00011,
#: so its attachments are not available" — `tools/files._require_parent_read`,
#: which is correct code refusing on a permission the account genuinely does not
#: hold. `Employee` belongs to Frappe HR; `roles.py` rule 1 forbids this app
#: writing a Custom DocPerm on another app's doctype, because one Custom DocPerm
#: makes Frappe ignore EVERY standard permission that doctype has, for every role
#: on the site, silently, during `bench migrate`. So this app cannot grant it.
#:
#: v0.62.0 ANSWERED THAT WITH A COMPANION ROLE AND THE ANSWER IS INCOMPLETE.
#: `create_mobile_user` assigns Frappe HR's own `HR User` alongside `Farm Manager`
#: — see `roles.py` — which works, and closes on exactly the sites and exactly the
#: accounts where it can run. It cannot run in two cases. A bench with no `hrms`
#: installed HAS NO `HR User` ROLE at all: enrolment reports it in
#: `companion_roles_missing` and carries on, and no amount of re-enrolling
#: conjures the role. And an account enrolled BEFORE v0.62.0 never received it,
#: because enrolment is a one-time write. The owner's account is the second case.
#:
#: WHY BROKERING IS NOT A WIDENING HERE. The three gates `_attachment_parent`
#: runs before this is consulted are STRICTER than the one being skipped, not
#: looser. Frappe's DocPerm asks one question — may this account read this
#: doctype — and answers it for the whole table. This surface asks three: the
#: parent has to be on `ATTACHMENT_PARENTS` at all, `Employee` is flagged True so
#: `employee.HR_ROLES` rides with it (System Manager, HR Manager, HR User, Farm
#: Manager — a Field Worker, Foreman or Crew Leader is refused here and would be
#: refused by Frappe too), and `require_scoped_doc` refuses any docname outside
#: the companies this caller's Mobile Access Grant names. That last one is a
#: scope Frappe's model cannot express without a User Permission per row, and it
#: is the reason the phrase "only company-scoped employees" is true of this door
#: and is not true of the DocPerm it stands in for.
#:
#: THE FOUR PARENTS THAT ARE DELIBERATELY ABSENT ARE THE POINT OF THE SET.
#:
#: `Farm Payroll Entry` — NEVER. One run holds a slip for every person on it, its
#: DocPerms are System Manager / HR Manager / HR User by design, and brokering it
#: would put the whole crew's wages in front of every Farm Manager. A worker's own
#: stub does not come through this door at all: `get_my_pay_stub_pdf` carries its
#: bytes in its own answer and matches the ONE file that is theirs by name.
#:
#: `Farm Incident Record` — NO. `ATTACHMENT_PARENTS` flags it True and its own
#: DocPerms are narrower still (System Manager, HR Manager), which v0.96.0 chose
#: on purpose: that entry "opens the door for the accounts that hold those roles
#: and does not manufacture a permission for anybody else". Brokering it would
#: manufacture exactly that permission and make the sentence false.
#:
#: `I-9 Form` — UNNECESSARY. `roles.HIRING_FORMS` already grants Farm Manager read
#: and write on it through this app's own permission table, which rule 1 permits
#: because the I-9 Form doctype is THIS APP'S. There is nothing to broker.
#:
#: EVERY `False` PARENT — UNNECESSARY, and this is worth checking rather than
#: assuming. Farm Task, Farm Task Assignment, Housing Unit, Housing Inspection,
#: Compliance Alert, Farm Shift and Farm Task Template are all doctypes `roles.py`
#: grants the phone roles read on directly, so `_require_parent_read` already
#: passes for them and adding one here would change nothing except the number of
#: places a future reader has to check.
BROKERED_PARENTS = frozenset({EMPLOYEE})


def _attachment_parent(doctype, docname, allowed: list) -> tuple:
	"""One parent document, proved readable by this caller. Returns (doctype, name).

	THREE GATES, IN THIS ORDER. The doctype has to be one on `ATTACHMENT_PARENTS`;
	a personnel parent brings the HR role with it; and the docname has to name a
	record inside the caller's own entities, which reads as not found when it does
	not — the same refusal `require_scoped_doc` gives everywhere else, so a caller
	cannot map the site's docnames by watching which error comes back.
	"""
	wanted = str(doctype or EMPLOYEE).strip() or EMPLOYEE
	if wanted not in ATTACHMENT_PARENTS:
		frappe.throw(
			f"{wanted} is not a record this surface reads attachments from. The ones it does "
			"are: " + ", ".join(sorted(ATTACHMENT_PARENTS)) + ". Nothing was read.",
			frappe.PermissionError,
		)
	if ATTACHMENT_PARENTS[wanted]:
		personnel.require_hr_role()
	compat.require_doctype(
		wanted,
		"It is not installed on this site, so nothing is filed against it.",
	)
	if wanted == HOUSING_UNIT:
		# A Housing Unit calls its company `owning_entity`, so `require_scoped_doc`
		# finds no `company` column and would let one through unscoped. The same
		# hand-made check `_house_one_person` makes, for the same reason.
		name = guard.require_docname(wanted, docname, "docname")
		owner = str(frappe.db.get_value(wanted, name, "owning_entity") or "")
		if owner and owner not in set(allowed):
			frappe.throw(f"docname {name} was not found.", frappe.DoesNotExistError)
		return wanted, name
	return wanted, guard.require_scoped_doc(wanted, docname, "docname", allowed)


# ── 50. list_attachments ────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_attachments", limit=guard.READ_LIMIT)
def list_attachments(user: str, doctype=None, docname=None) -> dict:
	"""What is filed against one record. The missing half of every upload here.

	v0.62.0, AND THE GAP IS SIX RELEASES OLD. This surface has published a way to
	FILE a document against an Employee since v0.48.3 and no way to ask what was
	already there — so a badge issued on a hire day was never visible from a
	handset again, the wizard could not show a returning picker the licence
	photograph it took last season, and "is there work authorization on file for
	this person" was a question only answerable in the Desk.

	IT DELEGATES TO `files.list_attachments`, which is the tool that has existed
	since v0.1 and which checks Frappe's own `read` permission on the parent —
	the one family of tools in this app that does, and `tools/files.py` argues why
	at length. That check runs on the WORKER, not on the MCP System User: on this
	transport `frappe.session.user` is the person holding the phone for the whole
	call, which is what makes the tool's promise about `is_private` mean something
	here.

	v0.100.1: EXCEPT ON `Employee`, WHERE THIS SURFACE ANSWERS FOR THE READ ITSELF.
	`BROKERED_PARENTS` is the one-entry set and carries the whole argument. The
	short version: `Employee` belongs to Frappe HR, this app may not write a
	DocPerm on it, the companion role v0.62.0 assigns cannot be assigned on a
	bench without `hrms` and was never assigned to an account enrolled before
	that release — and the three gates below are stricter than the DocPerm being
	stood in for, because one of them is a company scope Frappe's model cannot
	express. Nothing else on the list is brokered, and the four that are
	deliberately not are named there.

	THE PARENT DOCTYPE IS A CLOSED LIST AND A PERSONNEL PARENT CARRIES THE HR
	ROLE. See `ATTACHMENT_PARENTS`. The tool takes any doctype on the site, which
	is right on a console and would be a way to walk the File table from an
	orchard; this names the records the app actually files against, and gates the
	two that are somebody's identity documents.

	`docname` IS THE HANDSET'S SPELLING and `name` is the tool's. Both are
	accepted, because `AttachmentAPI.list` sends the first and the MCP tool
	documents the second, and a method that took only one of them would be a 400
	for whichever caller guessed wrong.

	`document_kind` IS NOT IN THE ANSWER AND CANNOT BE. `attach_onboarding_document`
	records it on the audit row rather than on the File — it is a label on the act,
	not a column on the object — so reporting one here would mean inventing it.
	The client treats it as optional and shows the filename instead, which is the
	honest fallback.
	"""
	allowed = guard.require_scope(user)
	parent, name = _attachment_parent(doctype, docname, allowed)

	if parent in BROKERED_PARENTS:
		# The three gates above have already answered a stricter question than the
		# one `files.list_attachments` would ask. See `BROKERED_PARENTS`.
		data = file_tools.list_attachments_on_authorized_parent(parent, name).data
	else:
		data = file_tools.list_attachments({"doctype": parent, "name": name}).data
	rows = []
	for row in data.get("attachments") or []:
		rows.append(
			{
				"name": row.get("name"),
				"file_name": row.get("file_name"),
				"file_url": row.get("file_url"),
				"file_size": row.get("file_size"),
				"size_human": row.get("size_human"),
				"is_private": bool(row.get("is_private")),
				# `content_type` is what `EmployeeAttachment` decodes and
				# `mime_type` is what the tool calls it. The same string twice
				# rather than a rename in either direction.
				"content_type": row.get("mime_type"),
				"mime_type": row.get("mime_type"),
				"creation": str(row.get("creation") or "") or None,
				"uploaded_by": row.get("uploaded_by"),
				"attached_to_field": row.get("attached_to_field"),
				# Whether `get_attachment_content` can hand this one back in one
				# piece. A 40 MB scan is listed and is not openable on a phone,
				# and saying so in the list is what stops the viewer trying.
				"retrievable": bool(row.get("retrievable")),
			}
		)
	return {
		"doctype": parent,
		"docname": name,
		# The tool's spelling of the same value, so a caller written against the
		# MCP tool's answer reads the same document.
		"name": name,
		"attachments": rows,
		"count": len(rows),
		"total_size": data.get("total_size"),
		"total_size_human": data.get("total_size_human"),
	}


# ── 51. get_attachment_content ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_attachment_content", limit=guard.UPLOAD_LIMIT)
def get_attachment_content(user: str, file=None, name=None, max_bytes=None) -> dict:
	"""One attachment's bytes, base64. Without it the list above cannot be opened.

	v0.62.0. NOT SUGAR OVER `file_url`, WHICH THIS APP CANNOT USE — and that is
	the whole reason this method exists rather than the client following the URL
	the list hands it. Every file this app writes is PRIVATE by design (an I-9
	document photograph must not be world-readable), the handset authenticates to
	the sidecar with `X-FarmOps-Token` rather than to Frappe, and a
	`/private/files/…` link answers that with a login page. It is the same failure
	`attach_onboarding_document` was built for, read backwards.

	THE FILE IS AUTHORIZED TWICE AND THE SECOND CHECK IS THE ONE THAT MATTERS.
	`files.get_attachment_content` asks the File's own parent for `read` and then
	the File doctype's own controller — Frappe's permissions, on the worker. This
	wrapper then re-runs `_attachment_parent` on the parent it is actually attached
	to, because a File docname is a global handle: without that, a caller could
	name the File id of a document hanging off another entity's Employee, or off a
	Journal Entry, and the tool alone would decide it on Frappe roles that a Farm
	Manager scoped to one company legitimately holds.

	v0.100.1: ON `Employee` THERE IS ONLY THE SECOND CHECK, WHICH IS THE ONE THIS
	DOCSTRING ALREADY CALLED THE ONE THAT MATTERS. See `BROKERED_PARENTS`. The
	brokered reader is handed the parent `_attachment_parent` just proved AND the
	File docname, and refuses unless the file actually hangs off that parent — so
	the global-handle problem the paragraph above is about is closed by the same
	argument on both paths, rather than by the DocPerm on either.

	AN UNATTACHED FILE IS REFUSED HERE, where the tool allows its owner to read
	one. There is no parent to scope it by and nothing on this surface produces
	one — `finalize_staged_file` commits evidence unattached on purpose, and
	`attach_onboarding_document` is the call that gives it a home. A file with no
	home is not this door's to open.

	`file` IS THE HANDSET'S SPELLING and `name` is the tool's; both are accepted,
	the same tolerance `list_attachments` has. Note it is the File DOCNAME, not the
	filename — `list_attachments` is where it comes from.

	IT IS RATE-LIMITED AS AN UPLOAD, not as a read. A viewer opening a folder of
	six photographs is six calls of a megabyte each, which is the shape
	`UPLOAD_LIMIT` was sized for; `READ_LIMIT` is for a list refreshing.
	"""
	allowed = guard.require_scope(user)
	docname = str(file or name or "").strip()
	if not docname:
		frappe.throw(
			"file is required — it is the File docname, which list_attachments gives. Nothing was read.",
			frappe.ValidationError,
		)
	docname = guard.require_docname("File", docname, "file")

	row = (
		frappe.db.get_value(
			"File", docname, ["attached_to_doctype", "attached_to_name", "is_folder"], as_dict=True
		)
		or {}
	)
	if row.get("is_folder"):
		frappe.throw(f"file {docname} is a folder, not a document.", frappe.ValidationError)
	parent_doctype = str(row.get("attached_to_doctype") or "")
	parent_name = str(row.get("attached_to_name") or "")
	if not parent_doctype or not parent_name:
		# See the docstring. Not "not found" — this one is a real refusal about a
		# real file, and a caller holding a token from `finalize_staged_file` needs
		# to be told to file it rather than to go looking for a different docname.
		frappe.throw(
			f"file {docname} is attached to no document, so there is nothing to check it "
			"against. File it against a record first — attach_onboarding_document does that "
			"for an Employee. Nothing was read.",
			frappe.PermissionError,
		)
	# The gate the tool cannot run: a File docname is global, and whose record it
	# hangs off is this surface's question rather than Frappe's.
	parent, name_on_parent = _attachment_parent(parent_doctype, parent_name, allowed)

	if parent in BROKERED_PARENTS:
		# Same brokering `list_attachments` does one method up, and the file is
		# still checked against the parent that was just proved — see
		# `files.attachment_content_on_authorized_parent`, which refuses a File
		# that hangs off anything else. The list and the open have to agree about
		# this or the folder would show a document that would not open.
		data = file_tools.attachment_content_on_authorized_parent(
			parent, name_on_parent, docname, max_bytes if max_bytes not in (None, "") else None
		).data
	else:
		inner = {"name": docname}
		if max_bytes not in (None, ""):
			inner["max_bytes"] = max_bytes

		data = file_tools.get_attachment_content(inner).data
	return {
		"name": data.get("name"),
		"file": data.get("name"),
		"file_name": data.get("file_name"),
		"file_url": data.get("file_url"),
		"is_private": data.get("is_private"),
		"attached_to_doctype": data.get("attached_to_doctype"),
		"attached_to_name": data.get("attached_to_name"),
		"file_size": data.get("file_size"),
		"size_human": data.get("size_human"),
		# THREE SPELLINGS OF TWO FACTS, and none of them is a rename. The client
		# reads `content_type` and `content`; the MCP tool answers `mime_type` and
		# `content_base64`; `encoding` says which of the two the bytes are in, so
		# nothing has to infer it from the key it happened to read.
		"content_type": data.get("mime_type"),
		"mime_type": data.get("mime_type"),
		"encoding": data.get("encoding"),
		"content": data.get("content_base64"),
		"content_base64": data.get("content_base64"),
	}


# ── 52. get_document_preview ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("get_document_preview", mutating=True, limit=guard.UPLOAD_LIMIT)
def get_document_preview(
	user: str, document_type=None, document_name=None, docname=None, employee=None, refresh=None
) -> dict:
	"""The unsigned page, as bytes, for the step that has to come before the pad.

	v0.63.0, AND `API_CONTRACT.md` §17.5 IS THE WHOLE ARGUMENT FOR IT. Step 1 of
	the signing evidence chain is that the signer SAW the form. The app has been
	unable to show it to them: `generate_i9_pdf` and `generate_w4_pdf` answer with
	a private `file_url`, this door authenticates with `X-FarmOps-Token` rather
	than to Frappe, and a `/private/files/…` link is a login page to it. So the
	presentation screen could print the section, the box and the verbatim
	attestation off `request_for_alert` and could not render the page any of it
	was about. §17.5 called that a server-side gap and said the fix is one route.
	This is the route.

	THE BYTES TRAVEL IN THE ANSWER, which is the same answer `submit_form_signature`
	gives on the way out and `get_employee_badge_pass` gives for a `.pkpass`, and
	for the identical reason. `content`, `content_base64` and `base64` are three
	spellings of one string — the contract's, the file tools' and the signature
	answer's — so a client written against any of the three reads the page.

	IT IS READ-ONLY IN THE SENSE THAT MATTERS AND `mutating=True` ANYWAY. No
	signature is taken, no signature column is written and the Signing Evidence
	register is not touched. What it can write is the rendered page itself, once,
	where the record has none — which on a fresh I-9 is every time, and without it
	this route would answer "no page" on the exact case the pad opens for. See
	`signed_documents.get_document_preview`, which argues why a caller asking for
	a preview by name is the same decision `_redraw`'s `ensure` flag is.

	`stale` IS THE KEY THE PRESENTATION STEP BRANCHES ON. True means the record
	has changed since the page was drawn, so the page is not what the record says
	now — and the fingerprint taken at signing covers the RECORD. Showing a stale
	page to a signer means hashing something other than what they read. `refresh`
	redraws; it is not the default, because a preview that re-rendered on every
	screen open would repoint `generated_pdf` a dozen times a hire day and that
	field is the copy somebody printed.

	A HIRING ROLE, BECAUSE THIS IS THE READ A PAD MAKES BEFORE ANYBODY SIGNS.
	v0.94.0. The previous docstring here argued its own replacement: it required
	the HR role while observing, in its last clause, that "the account holding the
	pad is the foreman's, not the signer's". Both of those cannot be true, and the
	second one is — so a preview gated on HR meant the foreman could not put the
	document in front of the worker to be signed, which is step 4 of a hire.

	THERE IS STILL NO "THEIR OWN" TO MAKE AN EXCEPTION FOR. This is addressed at
	a form by docname rather than at a person, so unlike `get_i9_form` it carries
	no self-service branch; `require_hiring_role` plus the company scope is the
	whole gate, and a picker is refused by it exactly as before.

	Every refusal is the tool's: a form with no signature line, a doctype this app
	does not render, a destroyed I-9, and Frappe's own `read` permission on the
	record. None of it is restated here.
	"""
	allowed = guard.require_scope(user)
	personnel.require_hiring_role()

	wanted = str(document_type or "").strip()
	if not wanted:
		frappe.throw(
			"document_type is required — the form to preview: 'I-9 Form', 'W-4 Form' or "
			"'Tax Form'. The alert or task the pad was opened from carries it in "
			"signature_request.doctype. Nothing was read.",
			frappe.ValidationError,
		)

	inner = {"document_type": wanted}
	for key, value in (
		("document_name", document_name or docname),
		("employee", employee),
		("refresh", refresh),
	):
		if value not in (None, ""):
			inner[key] = value
	if not (inner.get("document_name") or inner.get("employee")):
		frappe.throw(
			"document_name is required — the record to preview. The alert or task carries it "
			"in signature_request.docname; employee= finds the form by the person it belongs "
			"to instead. Nothing was read.",
			frappe.ValidationError,
		)

	# THE ENTITY GATE, AFTER THE RESOLUTION AND BEFORE THE READ. The tool takes an
	# employee or a docname and resolves either to a form; which ENTITY that form
	# belongs to is this surface's question rather than Frappe's, and asking it
	# against the resolved docname is what stops `employee=` being a way to reach
	# a record in a company this caller's User Permissions do not name. It reads
	# as not found, the same refusal `require_scoped_doc` gives everywhere.
	#
	# BEFORE, NOT AFTER, EVEN THOUGH THIS IS A READ. The preview draws the page
	# where the record has none, so a gate that ran on the way out would refuse
	# the bytes having already rendered and attached a File to a form in a company
	# this caller may not reach. The refusal has to land with nothing of theirs on
	# the record, which is the same order `seal_signed_document` keeps below.
	resolved_doctype, resolved_name = signed_documents.resolve_document(inner)
	guard.require_scoped_doc(resolved_doctype, resolved_name, "document_name", allowed)

	inner["document_type"] = resolved_doctype
	inner["document_name"] = resolved_name
	inner.pop("employee", None)
	data = signed_documents.get_document_preview(inner).data

	return {
		"document_type": data.get("document_type"),
		"document_name": data.get("document_name"),
		"docname": data.get("document_name"),
		"employee": data.get("employee"),
		"status": data.get("status"),
		"available": bool(data.get("available")),
		"rendered": bool(data.get("rendered")),
		"stale": bool(data.get("stale")),
		"modified": data.get("modified"),
		"file_url": data.get("file_url"),
		"file_name": data.get("file_name"),
		"content_type": data.get("content_type"),
		"encoding": data.get("encoding"),
		"content": data.get("content"),
		"content_base64": data.get("content_base64"),
		"base64": data.get("base64"),
		"bytes": data.get("bytes"),
		# What can be signed on this form and what already has been. The pad needs
		# both before it asks anybody to draw anything — see `_boxes_for` — and the
		# attestation on each is the government's own sentence, which §17.5 says
		# the presentation step shows verbatim rather than summarising.
		"signature_boxes": data.get("signature_boxes") or [],
		"note": data.get("note") or None,
	}


# ── 53. seal_signed_document ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("seal_signed_document", mutating=True, limit=guard.UPLOAD_LIMIT)
def seal_signed_document(
	user: str, document_type=None, document_name=None, docname=None, employee=None, include_pdf=None
) -> dict:
	"""Produce the tamper-evident copy of a form that has already been signed.

	v0.63.0. STEP 5 OF THE CHAIN, published here so a handset can take it — and
	`submit_form_signature` already takes it automatically, so the ordinary flow
	never needs this call. What it is for is the two cases the automatic step
	cannot cover: a form signed before v0.63.0, and a form whose second signature
	arrived through the Desk rather than through the pad.

	IT COLLECTS NOTHING AND SIGNS NOTHING. Every signature it seals is already on
	the record; this redraws the form — which stamps those captures into the page
	content and flattens the AcroForm away — appends the verification page built
	from the Signing Evidence rows, hashes the finished file, and files that hash
	back on the rows. An unsigned form is REFUSED rather than sealed, because a
	verification page on a form nobody signed is an official-looking appendix that
	vouches for nothing.

	`include_pdf` HANDS THE SEALED BYTES BACK AND DEFAULTS ON, for the reason
	`submit_form_signature`'s does: the file is private, this door cannot follow a
	private URL, and a route that produced an artefact the caller cannot open
	would have produced it for nobody.

	A HIRING ROLE, AND THE TOOL STILL CHECKS Frappe's own `write` permission on
	the form. v0.94.0: sealing is the last beat of the signing chain that
	`collect_signature` and `submit_form_signature` begin, and a gate that let the
	foreman collect and submit but not seal would leave the tamper-evident copy
	unmade — the evidence weaker for the sake of a role check two calls late.

	WHAT PROTECTS THE SEAL IS NOT THE ROLE. The signature it seals already passed
	the per-box roster; the form's own `write` permission is checked one layer
	down; and the Signing Evidence row this stamps names the account that made it.
	"""
	allowed = guard.require_scope(user)
	personnel.require_hiring_role()

	wanted = str(document_type or "").strip()
	if not wanted:
		frappe.throw(
			"document_type is required — the form to seal: 'I-9 Form', 'W-4 Form' or "
			"'Tax Form'. Nothing was changed.",
			frappe.ValidationError,
		)
	inner = {"document_type": wanted}
	for key, value in (("document_name", document_name or docname), ("employee", employee)):
		if value not in (None, ""):
			inner[key] = value
	if not (inner.get("document_name") or inner.get("employee")):
		frappe.throw(
			"document_name is required — the record to seal. employee= finds the form by the "
			"person it belongs to instead. Nothing was changed.",
			frappe.ValidationError,
		)

	# SCOPED BEFORE THE WRITE, unlike the preview, which scopes after. This one
	# attaches a file and stamps evidence rows, so a caller who may not reach the
	# entity must be refused with nothing of theirs having landed on it.
	resolved_doctype, resolved_name = signed_documents.resolve_document(inner)
	guard.require_scoped_doc(resolved_doctype, resolved_name, "document_name", allowed)

	data = signed_documents.seal_signed_document(
		{"document_type": resolved_doctype, "document_name": resolved_name}
	).data

	out = {
		"document_type": data.get("document_type"),
		"document_name": data.get("document_name"),
		"docname": data.get("document_name"),
		"sealed": bool(data.get("sealed")),
		"file_url": data.get("file_url"),
		"file_name": data.get("file_name"),
		"content_type": "application/pdf",
		"bytes": data.get("bytes"),
		"sealed_pdf_hash": data.get("sealed_pdf_hash"),
		"signatures_on_page": data.get("signatures_on_page"),
		"evidence": data.get("evidence") or [],
		"evidence_updated": data.get("evidence_updated") or [],
		"note": data.get("note") or None,
	}
	if _as_flag(include_pdf, default=True):
		out.update(_sealed_bytes(data.get("file_url")))
	return out


def _sealed_bytes(url) -> dict:
	"""The sealed copy read back as base64, or the keys saying it could not be.

	NEVER RAISES, for the reason `_signed_pdf` never does: the seal is on the
	document and the register points at it whatever happens here, and a failure to
	read a file back is not a failure of the write that produced it.
	"""
	out = {"encoding": None, "content": None, "content_base64": None, "base64": None}
	target = str(url or "").strip()
	if not target:
		return out
	try:
		docname = str(frappe.db.get_value("File", {"file_url": target}, "name") or "")
		content = file_tools.read_file_bytes(docname) if docname else b""
	except Exception:  # pragma: no cover - see the docstring
		return out
	if not content:
		return out
	encoded = base64.b64encode(content).decode("ascii")
	return {
		"encoding": "base64",
		"content": encoded,
		"content_base64": encoded,
		"base64": encoded,
	}


# ── 54. universal_scan ──────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("universal_scan", mutating=True, limit=guard.READ_LIMIT)
def universal_scan(
	user: str,
	content=None,
	scan=None,
	raw=None,
	code=None,
	company=None,
	shift=None,
	gps_lat=None,
	gps_lon=None,
	history_limit=None,
) -> dict:
	"""One camera, four registers, one call. v0.65.0.

	THE SCANNER SCREEN HAD TO KNOW THE ANSWER BEFORE IT COULD ASK THE QUESTION.
	`resolve_badge`, `scan_asset`, `get_housing_unit` and `get_field` each refuse
	everything that is not theirs, so a phone pointed at an unknown QR either
	asked the worker which kind of thing they were about to scan, or called all
	four and read the refusals. The server knows which register a string is in;
	this is that knowledge published as a route.

	IT IS METERED AS A READ AND DECLARED AS A WRITE, and both are deliberate.
	The only write on any branch is the `last_scan_at` stamp `scan_asset` leaves
	on the asset the worker is standing in front of — so the route table records
	it as mutating, because it is. The LIMIT is `resolve_badge`'s sixty rather
	than `scan_asset`'s ten because of what this route is used for: a crew clock
	scanning a queue at a bin trailer is forty badge reads in a minute, every one
	of them a pure read, and `WRITE_LIMIT` would refuse the crew rather than the
	abuse.

	THE COMPANY IS THE CALLER'S, ALWAYS. It is taken from the scope check rather
	than from the body, so a tag belonging to another entity resolves as though
	it were not there — the same answer `resolve_badge` gives a card from another
	site, and the reason a scan cannot be used to enumerate the register next
	door. `company` in the body is honoured only as a NARROWING of what this
	account already reaches; `guard.require_company` refuses anything else.

	GPS IS PASSED THROUGH AND LANDS ON ONE BRANCH. An asset scan records where
	the worker was standing; a badge, a cabin and a block scan record nothing at
	all, and `scan_recorded` in the answer says which happened rather than
	leaving a client to infer it from the entity type.

	THE FOUR SPELLINGS OF `content` ARE THE TOOL'S OWN, restated in this
	signature because this transport's argument filter keeps only the keys a
	signature declares — a handset posting `code` at a method that names only
	`content` would arrive with an empty scan and be told the field is required.
	"""
	allowed = guard.require_scope(user)
	scanned = str(content or scan or raw or code or "").strip()
	if not scanned:
		frappe.throw("content is required — the string the scanner read.", frappe.ValidationError)

	inner = {
		"content": scanned,
		"company": _company(user, company, allowed),
		"scanned_by": user,
	}
	if shift:
		inner["shift"] = guard.require_scoped_doc(FARM_SHIFT, shift, "shift", allowed)
	if gps_lat is not None:
		inner["gps_lat"] = gps_lat
	if gps_lon is not None:
		inner["gps_lon"] = gps_lon
	if history_limit is not None:
		inner["history_limit"] = history_limit

	data = universal_scan_tool.universal_scan(inner).data
	# The belt to the tool's own braces: every list that leaves here is checked
	# against the caller's entities on the way out, exactly as
	# `list_compliance_alerts` checks its rows. A task or an alert that escaped
	# the company filter through a code path nobody thought about is the failure
	# this surface exists to prevent.
	for key in ("pending_tasks", "overdue_tasks", "due_compliance"):
		data[key] = guard.scoped(data.get(key) or [], allowed)
	data["pending_task_count"] = len(data["pending_tasks"])
	data["overdue_task_count"] = len(data["overdue_tasks"])
	data["due_compliance_count"] = len(data["due_compliance"])
	return data


# ════════════════════════════════════════════════════════════════════════════
# v0.67.0 — RECEIPT CAPTURE
#
# Four methods, one screen. The app's capture flow is: photograph → on-device
# OCR → `classify_receipt` → the create call for whichever register came back.
# That is the whole of "the receipt is the financial atom" as a phone sees it,
# and the branch is the only part of it that is not identical across the four
# kinds of paper a foreman photographs.
#
# WHAT IS DELIBERATELY *NOT* PUBLISHED HERE, and each for its own reason:
#
#   `submit_scale_ticket` — submitting freezes a third party's weight record.
#     A phone captures; a person at a desk who can see the settlement it will be
#     checked against decides when it stops being editable. The MCP tool exists
#     for that person and carries its own switch.
#   `create_settlement_statement` / `submit_settlement_statement` — a settlement
#     is a multi-page document that arrives by post or email at an office. It is
#     not a thing anybody photographs at a tailgate, and a create call with two
#     child tables in its body is not a capture, it is data entry.
#   `approve_expense_receipt` / `reject_expense_receipt` — approval is not a
#     field action, and it never was: v0.31.0 put those behind separate switches
#     for the same reason this transport leaves them off entirely.
#
# `classify_receipt` IS PUBLISHED THOUGH IT TOUCHES NOTHING. It reads no
# doctype, writes nothing, and could in principle ship as a table inside the
# app. It is here because the table would then exist twice — once in
# `tools/receipts.py` and once in Swift — and the two copies would drift apart
# the first time somebody added a keyword on one side. The classification a
# phone shows and the classification the catalogue makes are the same function
# call, or they are two answers to one question.
# ════════════════════════════════════════════════════════════════════════════


# ── 55. classify_receipt ────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("classify_receipt", limit=guard.READ_LIMIT)
def classify_receipt(user: str, merchant=None, description=None, text=None, amount=None) -> dict:
	"""Which register a photographed document belongs in. v0.67.0.

	Metered as a READ and declared as one, because it is: no doctype is touched
	on any branch. The rate limit is `READ_LIMIT` rather than `WRITE_LIMIT`
	because of what the screen does with it — a foreman working through a
	glovebox of slips at the end of a week is thirty classifications in a
	minute, none of which writes anything, and ten would refuse the person
	rather than the abuse.

	THE ANSWER IS A SUGGESTION AND THE APP IS TOLD SO. `confidence` is never 1.0
	and `matched_signals` comes back with every answer, so the capture screen can
	pre-select a tab AND show why. A classifier whose reasoning is invisible is a
	classifier nobody corrects, and every correction a person makes here is
	training data for the keyword table in a way a hidden score never is.
	"""
	guard.require_scope(user)

	inner = {}
	for key, value in (
		("merchant", merchant),
		("description", description),
		("text", text),
		("amount", amount),
	):
		if value not in (None, ""):
			inner[key] = value

	return receipt_tools.classify_receipt(inner).data


# ── 56. create_expense_receipt ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_expense_receipt", mutating=True, limit=guard.WRITE_LIMIT)
def create_expense_receipt(
	user: str,
	merchant=None,
	amount=None,
	receipt_date=None,
	category=None,
	company=None,
	supplier=None,
	farm_task=None,
	status=None,
	receipt_image=None,
	ocr_raw_text=None,
	ocr_confidence=None,
	items=None,
	notes=None,
	card_last_four=None,
	merchant_phone=None,
	merchant_url=None,
	store_number=None,
	cost_center=None,
) -> dict:
	"""The fuel slip at the pump, with v0.67.0's Supplier and Item links.

	`submitted_by` IS THE AUTHENTICATED CALLER AND IS NOT AN ARGUMENT. The tool
	takes one, and a phone that could name somebody else in a request body could
	file an expense against another worker's name — which is a reimbursement
	claim with the wrong person's signature on it. Same rule as
	`list_dispatched_tasks`' `worker_id`, and the same reason.

	`status` IS forwarded, unlike `create_employee`'s. The tool's own
	`CREATABLE_STATUSES` admits Draft and Submitted and nothing else, so the
	worst a phone can do with it is post something it has not finished — which
	is exactly what an offline queue draining on a truck's hotspot needs to do.
	Approved and Rejected are refused by the tool, not by this signature.

	`supplier` and `items[].item` ARE FORWARDED AND NEVER INFERRED. A picker in
	the app puts a Supplier against a merchant when the person capturing
	recognises one; nothing here fuzzy-matches `VALLEY CO-OP #14` onto a
	Supplier record, because a wrong link is worse than no link and is
	indistinguishable from a right one afterwards.

	v0.75.0 FORWARDS THE FOUR CAPTURE SIGNALS — `card_last_four`,
	`merchant_phone`, `merchant_url`, `store_number` — because the phone is where
	they are read. Vision has the full-resolution image in its hands; this app
	has a text blob the phone chose to send, and four anchored regexes over it.
	Every one of them is OPTIONAL, and `ocr_raw_text` alone still works: the tool
	reads whatever the phone did not send off the text itself.

	THE RESOLUTION TRIPLE IS DELIBERATELY NOT ON THIS SIGNATURE.
	`resolved_merchant`, `resolution_method` and `resolution_confidence` are a
	CALLER'S OWN JUDGEMENT and they short-circuit the whole cascade — which is
	the right shape for a desk client with a model in the loop, and the wrong
	one for a phone in a truck, where the same field would let a bad on-device
	guess overrule a mapping a bookkeeper taught by hand. The phone reports what
	it READ; deciding what that means stays on this side.
	"""
	allowed = guard.require_scope(user)

	inner = {
		"merchant": merchant,
		"amount": amount,
		"receipt_date": receipt_date,
		"company": _company(user, company, allowed),
		"submitted_by": _employee(user),
	}
	for key, value in (
		("category", category),
		("supplier", supplier),
		("cost_center", cost_center),
		("farm_task", farm_task),
		("status", status),
		("receipt_image", receipt_image),
		("ocr_raw_text", ocr_raw_text),
		("ocr_confidence", ocr_confidence),
		("notes", notes),
		("card_last_four", card_last_four),
		("merchant_phone", merchant_phone),
		("merchant_url", merchant_url),
		("store_number", store_number),
	):
		if value not in (None, ""):
			inner[key] = value
	if items:
		inner["items"] = _receipt_items(items)

	return expense_tools.submit_expense_receipt(inner).data


def _receipt_items(raw) -> list:
	"""The app's line-item list, checked into the shape the tool takes.

	A JSON string is accepted as well as a list because this transport hands the
	body through untouched and `URLSession` posting `application/json` and a
	`multipart` retry do not agree about nested arrays. The tool refuses anything
	that is not a list of objects, so a malformed body is still refused — this
	only spares the phone a 500 where the intent was unambiguous.
	"""
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			frappe.throw("items must be a JSON array of line objects.", frappe.ValidationError)
	if not isinstance(raw, list):
		frappe.throw("items must be a list of line objects.", frappe.ValidationError)
	return raw


# ── 57. create_scale_ticket ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_scale_ticket", mutating=True, limit=guard.WRITE_LIMIT)
def create_scale_ticket(
	user: str,
	ticket_number=None,
	date=None,
	customer=None,
	company=None,
	variety=None,
	grade=None,
	gross_weight=None,
	tare_weight=None,
	weight_uom=None,
	field=None,
	block=None,
	truck_id=None,
	driver=None,
	destination=None,
	ticket_image=None,
	notes=None,
) -> dict:
	"""The thermal slip at the tailgate, captured as a draft. v0.67.0.

	IT ARRIVES AS A DRAFT AND THERE IS NO `submit` ARGUMENT. Submitting makes a
	third party's weight record immutable, and the person who should decide that
	is the one who can see the settlement it will be checked against — not the
	foreman standing at a truck with a photograph. `submit_scale_ticket` exists
	in the catalogue for that person, behind its own switch, and is deliberately
	not published at this door.

	NET WEIGHT IS NOT AN ARGUMENT EITHER, here or in the tool. It is gross minus
	tare, computed by the controller. A phone that could post a net would be a
	phone that could post a net disagreeing with the two numbers beside it, and
	the disagreement is the single most valuable thing on the record: where the
	slip's own printed net differs from the subtraction, that goes in `notes`
	beside the photograph, where a person will read it.
	"""
	allowed = guard.require_scope(user)

	inner = {
		"ticket_number": ticket_number,
		"date": date,
		"customer": customer,
		"company": _company(user, company, allowed),
	}
	for key, value in (
		("variety", variety),
		("grade", grade),
		("gross_weight", gross_weight),
		("tare_weight", tare_weight),
		("weight_uom", weight_uom),
		("field", field),
		("block", block),
		("truck_id", truck_id),
		("driver", driver),
		("destination", destination),
		("ticket_image", ticket_image),
		("notes", notes),
	):
		if value not in (None, ""):
			inner[key] = value

	return receipt_tools.create_scale_ticket(inner).data


# ── 58. list_scale_tickets ──────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_scale_tickets", limit=guard.READ_LIMIT)
def list_scale_tickets(
	user: str,
	company=None,
	customer=None,
	status=None,
	unmatched=None,
	from_date=None,
	to_date=None,
	limit=None,
) -> dict:
	"""What this crew has already delivered, so the same load is not filed twice.

	The capture screen's back-button list. A foreman who has just photographed a
	ticket wants to see the last few they filed, and a duplicate ticket number
	against the same packer is the mistake this list prevents — the register does
	not refuse one, because two packers really do both have a ticket 4471.

	SCOPED TWICE, like every other list here. The tool filters by company; the
	rows are checked against the caller's entities again on the way out, because
	a row that escapes the filter through a code path nobody thought about is
	the failure this surface exists to prevent.
	"""
	allowed = guard.require_scope(user)
	wanted = guard.require_company(user, company, allowed)

	inner = {"limit": limit or MOBILE_TICKET_LIMIT}
	for key, value in (
		("company", wanted),
		("customer", customer),
		("status", status),
		("from_date", from_date),
		("to_date", to_date),
	):
		if value not in (None, ""):
			inner[key] = value
	if str(unmatched or "").lower() in ("1", "true", "yes"):
		inner["unmatched"] = True

	data = receipt_tools.list_scale_tickets(inner).data
	rows = guard.scoped(data.get("scale_tickets") or [], allowed)
	return {
		"scale_tickets": rows,
		"count": len(rows),
		"company": wanted or None,
		"total_net_weight": round(sum(float(row.get("net_weight") or 0) for row in rows), 3),
		"by_weight_uom": data.get("by_weight_uom") or {},
		"by_status": data.get("by_status") or {},
	}


# ────────────────────────────────────────────────────────────────────────────
# COMPLIANCE ALERT RECTIFICATION — Sprint 3 (v0.68.0)
# ────────────────────────────────────────────────────────────────────────────
#
# `api/rectify.py::describe_rectification` names, per alert type, which of these
# routes fixes it. Five are direct forms — one small write and the alert clears
# on the next sweep. The sixth, `rectify_alert`, is the one every task-shaped
# alert type shares: it raises the Farm Task the fix actually is and lets the
# claim/complete/evidence path that has shipped since Sprint 8 do the rest.


# ── 59. renew_certification ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("renew_certification", mutating=True, limit=guard.WRITE_LIMIT)
def renew_certification(
	user: str,
	certification=None,
	new_expiration=None,
	what_was_done=None,
	renewed_on=None,
	certificate_number=None,
	attached_certificate=None,
) -> dict:
	"""Move a certificate's expiration out, and record what earned the renewal.

	Answers the `certification_expiring` alert's rectification. `tools/evidence.py`
	keeps the previous term on the row rather than overwriting it — a renewal is
	an event, not a field edit — so this refuses the same way it does: a new
	date that does not move the expiration forward, or a `renewed_on` in the
	future.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(CERTIFICATION, certification, "certification", allowed)

	inner = {"certification": name, "new_expiration": new_expiration, "what_was_done": what_was_done}
	for key, value in (
		("renewed_on", renewed_on),
		("certificate_number", certificate_number),
		("attached_certificate", attached_certificate),
	):
		if value is not None:
			inner[key] = value

	result = evidence_tools.renew_certification(inner)
	return result.data


# ── 60. record_training ─────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("record_training", mutating=True, limit=guard.WRITE_LIMIT)
def record_training(
	user: str,
	employee=None,
	company=None,
	regimes=None,
	content_topics_covered=None,
	completed_date=None,
	expires_date=None,
	training_source=None,
	training_type=None,
	provider=None,
	completed_time=None,
	certificate_file=None,
	person_performed_signature=None,
	notes=None,
) -> dict:
	"""File one training event, tagged for every regime it answers.

	Answers the `training_expiring` alert's rectification. `training_expiring`
	fires on `expires_date`, not on a calendar date, so it goes away by itself
	the moment a newer record with a later expiry is filed here — no separate
	step closes the alert.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee, allowed)
	entity = _company(user, company, allowed)

	inner = {
		"employee": person,
		"company": entity,
		"regimes": regimes,
		"content_topics_covered": content_topics_covered,
		"completed_date": completed_date,
		"training_type": training_type,
	}
	for key, value in (
		("expires_date", expires_date),
		("training_source", training_source),
		("provider", provider),
		("completed_time", completed_time),
		("certificate_file", certificate_file),
		("person_performed_signature", person_performed_signature),
		("notes", notes),
	):
		if value is not None:
			inner[key] = value

	result = training_tools.record_training(inner)
	return result.data


# ── 61. sign_training_supervisor_review ─────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("sign_training_supervisor_review", mutating=True, limit=guard.WRITE_LIMIT)
def sign_training_supervisor_review(
	user: str,
	training_record=None,
	supervisor=None,
	reviewed_on=None,
	supervisor_signature=None,
	replace_reviewer=None,
) -> dict:
	"""Record the §112.161(b) supervisor review on one training record.

	Answers the `supervisor_review_lapsed` alert's rectification. The reviewer
	cannot be the person the record says was trained, and cannot be employed by
	a different entity than the record belongs to — both refused one layer down,
	in `tools/training.py`, exactly as they are from the Desk.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(TRAINING_RECORD, training_record, "training_record", allowed)
	reviewer = _employee_argument(supervisor, allowed, "supervisor")

	inner = {"record": name, "supervisor": reviewer}
	for key, value in (
		("reviewed_on", reviewed_on),
		("supervisor_signature", supervisor_signature),
		("replace_reviewer", replace_reviewer),
	):
		if value is not None:
			inner[key] = value

	result = training_tools.sign_training_supervisor_review(inner)
	return result.data


# ── 62. update_regulatory_filing ────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("update_regulatory_filing", mutating=True, limit=guard.WRITE_LIMIT)
def update_regulatory_filing(
	user: str,
	filing=None,
	filing_type=None,
	period_covered=None,
	docket_number=None,
	response=None,
	attached_filing=None,
	attached_response=None,
	notes=None,
	submission_date=None,
	response_due_date=None,
	response_received_date=None,
	agency=None,
	status=None,
) -> dict:
	"""Record the agency's response, the docket number, or the documents.

	Answers the `filing_response_due` alert's rectification. Filing
	`response_received_date` is what actually clears the alert — the sweep reads
	it as the thing being waited for having happened — and the tool layer says so
	back in the response.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(REGULATORY_FILING, filing, "filing", allowed)

	inner = {"filing": name}
	for key, value in (
		("filing_type", filing_type),
		("period_covered", period_covered),
		("docket_number", docket_number),
		("response", response),
		("attached_filing", attached_filing),
		("attached_response", attached_response),
		("notes", notes),
		("submission_date", submission_date),
		("response_due_date", response_due_date),
		("response_received_date", response_received_date),
		("agency", agency),
		("status", status),
	):
		if value is not None:
			inner[key] = value

	result = evidence_tools.update_regulatory_filing(inner)
	return result.data


# ── 63. advance_policy_review ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("advance_policy_review", mutating=True, limit=guard.WRITE_LIMIT)
def advance_policy_review(user: str, policy=None, review_due_date=None, notes=None) -> dict:
	"""Record that a procedure was reviewed, and move its next review date out.

	Answers the `policy_review_overdue` alert's rectification. A narrow door onto
	`update_compliance_policy`, which takes several more fields than a phone
	answering this one alert has any business changing — `policy_name`,
	`status`, the version chain — so only the two this alert is actually about
	are accepted here.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(COMPLIANCE_POLICY, policy, "policy", allowed)
	if not str(review_due_date or "").strip():
		frappe.throw("review_due_date is required.", frappe.ValidationError)

	inner = {"policy": name, "review_due_date": review_due_date}
	if notes is not None:
		inner["notes"] = notes

	result = evidence_tools.update_compliance_policy(inner)
	return result.data


# ── 64. rectify_alert ────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("rectify_alert", mutating=True, limit=guard.WRITE_LIMIT)
def rectify_alert(user: str, alert=None, confirm=None) -> dict:
	"""Raise the task one compliance alert's rectification says to raise.

	THE ONE ROUTE FOR EVERY TASK-SHAPED FIX. `api/rectify.py::describe_rectification`
	answers `action_type: "create_task"` (or a more specific verb — "start_inspection_session",
	"create_water_test", "log_shift_event" — that resolves to the same mechanism) for
	every alert whose fix is real-world work before it is a compliance record: walk
	the cabin, sample the water, test the detector, document the heat break. This
	is what a tap on one of those alerts calls.

	IT DOES NOT TAKE AN ACTION NAME. The mapping from alert to mechanism is decided
	SERVER-SIDE, by this alert's own `alert_type`, never by an argument the caller
	sends — the same reason no wrapper in this file takes a doctype and a docname
	and calls whatever tool a body names. `confirm` is required and changes nothing
	by itself; it exists so a client cannot raise a task by fetching the calendar
	and mis-tapping.

	RETURNS THE TASK, NOT THE COMPLIANCE RECORD. Completing it — with the
	evidence its `evidence_required` contract asks for — is `complete_task_via_mobile`,
	unchanged, because raising the task and doing the work it names are still two
	different moments.

	Refuses an alert this app has no task recipe for, and an alert with a more
	specific rectification than a task — `submit_w4`, `collect_form_signature`,
	`renew_certification` and the rest each have their own route above, because
	each already IS the whole fix and a task in front of it would be a step
	nobody needs.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(ALERT, alert, "alert", allowed)
	if not frappe.utils.cint(confirm):
		frappe.throw("confirm is required to rectify_alert. Nothing was changed.", frappe.ValidationError)

	row = compliance_calendar.get_compliance_alert({"alert": name}).data
	rectification = rectify.describe_rectification(row) or {}
	if not rectification.get("can_rectify_mobile"):
		frappe.throw(
			f"{name} has no fix this app can start from a phone. "
			+ str(rectification.get("explanation") or ""),
			frappe.ValidationError,
		)
	if rectification.get("action_endpoint") != "/farmops/api/mobile/rectify_alert":
		frappe.throw(
			f"{name}'s fix is {rectification.get('action_label')!r}, at "
			f"{rectification.get('action_endpoint')} — call that route instead of rectify_alert.",
			frappe.ValidationError,
		)

	result = dispatch.materialize_task_for_alert({"alert": name})
	updated = compliance_calendar.get_compliance_alert({"alert": name}).data
	return {
		"alert": shape.alert(updated),
		"action_type": rectification.get("action_type"),
		"task": result.data,
	}


# ── 65. validate_document ────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("validate_document", mutating=True, limit=guard.WRITE_LIMIT)
def validate_document(
	user: str,
	document_type=None,
	ocr_text=None,
	extracted_fields=None,
	source_doctype=None,
	source_name=None,
	scan_file_url=None,
	image_data=None,
	company=None,
	auto_store=None,
	llm_assessment=None,
	llm_model=None,
	expected_name=None,
) -> dict:
	"""The phone has read a label. This decides whether to believe it. v0.69.0.

	THE THIRD STAGE OF A PIPELINE WHOSE FIRST TWO ARE ON THE DEVICE. Vision
	reads the paper and on-device extraction pulls fields out of what it read;
	both are fast, both work offline, and neither can tell whether what it read
	is TRUE — `0` and `O` are the same shape at 200 dpi in a dusty chemical
	shed. This route runs the checks that need a rule rather than a camera, and
	`document_intel.py` is where every one of them lives.

	THE ROUTE IS `/farmops/api/mobile/validate_document`, not the hyphenated
	`/farmops/api/validate-document` the Sprint 4 contract named. This transport
	builds every path from the method's own name under `/mobile` (see
	`farmops_api/routes.py::Route`), a method name cannot carry a hyphen, and
	forking the router for two endpoints would break the closed-table invariant
	`test_farmops_api.py` asserts in both directions. The body and the answer
	are the contract's, unchanged.

	`image_data` IS ACCEPTED AND DELIBERATELY NOT STORED. It is in the contract
	because a client may hold the bytes before it holds a File; this route does
	not write a File and does not put base64 in the record. Stage the image with
	`stage_file_chunk`/`finalize_staged_file` and pass `scan_file_url`, which is
	the path every other image on this surface already takes.

	THE SOURCE RECORD IS SCOPE-CHECKED. `source_doctype`/`source_name` are how
	the name on a licence gets compared to the person it is filed against, which
	means an unscoped one would let a handset read employee names out of a
	refusal message by guessing docnames. A record belonging to an entity this
	account does not reach answers "not found", exactly as every other docname
	on this surface does. `company` is narrowed the same way: it may only name
	an entity this account already reaches, because the record it lands on is
	what a Company User Permission scopes every later read by.
	"""
	allowed = guard.require_scope(user)
	company = guard.require_company(user, company, allowed)

	if not str(document_type or "").strip():
		frappe.throw(
			"document_type is required — it decides which checks run, so there is no default.",
			frappe.ValidationError,
		)
	if extracted_fields in (None, "", {}):
		frappe.throw(
			"extracted_fields is required — what on-device extraction pulled out of the OCR text.",
			frappe.ValidationError,
		)

	inner = {"document_type": document_type, "extracted_fields": extracted_fields}

	if source_doctype:
		target = str(source_doctype).strip()
		if not compat.doctype_exists(target):
			frappe.throw(f"{target} is not a doctype on this site.", frappe.ValidationError)
		inner["source_doctype"] = target
		if source_name:
			inner["source_name"] = guard.require_scoped_doc(target, source_name, "source_name", allowed)
	elif source_name:
		frappe.throw(
			"source_name was given without source_doctype, so there is no register to look it up in.",
			frappe.ValidationError,
		)

	for key, value in (
		("ocr_text", ocr_text),
		("scan_file_url", scan_file_url),
		("company", company),
		("auto_store", auto_store),
		("llm_assessment", llm_assessment),
		("llm_model", llm_model),
		("expected_name", expected_name),
	):
		if value not in (None, ""):
			inner[key] = value

	return docvalidation.validate_document_extraction(inner).data


# ── 66. get_document_validation ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("get_document_validation", limit=guard.READ_LIMIT)
def get_document_validation(user: str, name=None, validation_id=None) -> dict:
	"""One stored validation, read back. v0.69.0.

	POST WITH THE DOCNAME IN THE BODY, not `GET /farmops/api/document-validation/<name>`
	as the Sprint 4 contract wrote it: every route on this transport is POST and
	the router matches whole paths rather than patterns, so a path parameter has
	nowhere to land. See `validate_document` on why the router was not forked.

	Carries the OCR text and the stored extraction, which the list does not —
	this is the call a client makes when somebody has chosen one validation to
	look at, and the raw text is half of what there is to look at.
	"""
	guard.require_scope(user)
	reference = str(name or validation_id or "").strip()
	if not reference:
		frappe.throw("name is required — a Document Validation docname.", frappe.ValidationError)
	guard.require_docname(DOCUMENT_VALIDATION, reference, "name")
	return docvalidation.get_document_validation({"name": reference}).data


# ════════════════════════════════════════════════════════════════════════════
# Sprint 7 (v0.72.0) — THE FOREMAN'S CREW-TASK DASHBOARD
# ════════════════════════════════════════════════════════════════════════════
#
# Five tools that have existed since Sprint 8 and have never been reachable from
# a handset: the board for somebody else's work, the dispatch that moves it, the
# task raised on the spot, and the two ends of the template register. Audited
# against v0.71.0 by the iOS session, which found no wrapper and no route for any
# of them.
#
# ALL FIVE CARRY `guard.require_dispatch_role`, WHICH NOTHING ABOVE THIS LINE
# DOES. Every method on this surface until now is a worker's own work — their
# tasks, their shift, their onboarding, the receipt they photographed — and the
# gate that fits it is `FARM_OPS_ROLES`, which admits a picker. These five are
# the other thing: reading a board that is not yours, and deciding whose
# afternoon a job lands in. `dispatch.py` already draws that line for Critical
# urgency on a field report and draws it between exactly these two roles, so this
# is the same line rather than a new one.
#
# THE TOOLS HAVE NO ROLE CHECK OF THEIR OWN, and that is why the gate is here
# rather than delegated the way `require_hr_role` is on the onboarding methods.
# `assign_farm_task`, `create_farm_task` and `create_task_from_template` reach
# `frappe`'s writer with `ignore_permissions=True` after checking their
# arguments and nothing else — on the MCP transport what stands in front of them
# is the operator's own tool-enablement switch, and a phone does not go through
# that switch. Publishing them here without the gate would put "take this job off
# Ana and give it to me" on every enrolled handset in the orchard.
#
# WHAT IS DELIBERATELY NOT PASSED THROUGH, on the three writes:
#
#   * `assigned_to_name` — the tools take it and write it onto the task AND onto
#     the assignment, in place of the name the Employee register holds. A phone
#     that can put arbitrary text where the dispatched worker's name goes can
#     make a dispatch record say somebody else was sent. The register has the
#     name; `_worker_name` reads it.
#   * `creates_record` and `creates_record_data` — which compliance record
#     completing the task produces, and the fields pre-filled into it. This is
#     `record_data` under another name and the answer is the same one
#     `complete_task_via_mobile` gives: the phone has no business composing it.
#     Work that must produce a Housing Inspection is work that comes off a
#     template, which is why `create_task_from_template` is in this set.
#   * `draft` — a task raised from a handset that lands in Draft is invisible to
#     every other handset, so the foreman standing in the block believes they
#     dispatched something and nobody can see it. Everything raised here is
#     published.
#   * `source_alert` — one task per alert is a rule with a refusal behind it, and
#     `rectify_alert` is the route that owns that link. A second door onto it
#     would be a second place for the one-per-alert rule to be got wrong.
#   * `materials_used` — the tank mix, which `complete_farm_task` draws down out
#     of stock. A spray task's mix is decided before anybody drives anywhere,
#     which is what a template is for.
#   * `worker_id` on the read — see `list_dispatched_tasks`, which declares
#     `employee` instead and checks it against the caller's own crew.


def _open_shifts_led_by(employee: str, allowed: list, company: str = "", shift: str = "") -> list:
	"""The shifts this person has OPEN and is the foreman of, newest first.

	Open is `end_datetime` unset, which is `shifts.status_for`'s own rule and not
	the stored `status` column — a shift ticked Closed with no end time is still
	being worked, and that ordering is settled in `shifts.py` rather than
	re-decided here. A cancelled shift is dropped: it has no end time either, and
	a crew that was stood down is not a crew whose board anybody is working from.
	"""
	if not compat.doctype_exists(FARM_SHIFT):
		return []
	filters = {
		"foreman": employee,
		"end_datetime": ("is", "not set"),
		"company": company if company else ("in", list(allowed)),
	}
	if shift:
		filters["name"] = shift
	rows = shift_records.rows(filters, limit=CREW_BOARD_CAP)
	return [row for row in rows if not compat.checked(row.get("cancelled"))]


def _crew_under(user: str, allowed: list, company: str, shift: str) -> tuple:
	"""(the open shifts this caller leads, the people whose boards they may read).

	THE CALLER IS ALWAYS IN THE ANSWER, whether or not they rostered themselves.
	A foreman is on the crew in every sense that matters to a dashboard — they
	take work too — and a board that showed everybody's tasks except the reader's
	own would be a board nobody trusts.

	A LEFT `left_at` DOES NOT REMOVE SOMEBODY. Whoever was clocked out at noon
	still holds whatever they were sent to that morning, and dropping them is how
	an unfinished job stops being anybody's. It is reported instead: `left_at` is
	on every crew entry, so the dashboard can grey the row rather than lose it.
	"""
	me = _employee(user)
	shift_rows = _open_shifts_led_by(me, allowed, company, shift)

	crew, seen = [], set()
	for row in shift_rows:
		for member in shift_records.crew_of(str(row.get("name") or "")):
			person = str(member.get("employee") or "").strip()
			if not person or person in seen:
				continue
			seen.add(person)
			crew.append(
				{
					"employee": person,
					"employee_name": member.get("employee_name") or person,
					"role": member.get("role") or "Worker",
					"shift": row.get("name"),
					"joined_at": str(member.get("joined_at") or "") or None,
					"left_at": str(member.get("left_at") or "") or None,
				}
			)
			if len(crew) >= CREW_BOARD_CAP:
				break
	if me not in seen:
		crew.insert(
			0,
			{
				"employee": me,
				"employee_name": str(frappe.db.get_value(EMPLOYEE, me, "employee_name") or "") or me,
				"role": "Foreman",
				"shift": (shift_rows[0].get("name") if shift_rows else None),
				"joined_at": None,
				"left_at": None,
			},
		)
	return shift_rows, crew


# ── 67. list_dispatched_tasks ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_dispatched_tasks", limit=guard.READ_LIMIT)
def list_dispatched_tasks(
	user: str,
	employee=None,
	shift=None,
	farm_shift=None,
	state=None,
	include_finished=None,
	company=None,
) -> dict:
	"""What the crew on this foreman's open shift is holding. v0.72.0.

	SCOPED TO THE CREW, NOT TO THE SITE. `dispatch.list_dispatched_tasks` reads
	one named worker's assignments and will read anybody's — it is an MCP tool
	behind an operator's own enablement switch, and "which worker" is the whole
	of its argument. On a handset that is not a scope: an account able to name
	anybody would be able to walk the payroll one docname at a time and read what
	every person on the farm is doing today. So the WORKERS ARE COMPUTED HERE
	rather than accepted — the caller's own open shifts, the crew rostered on
	them, and the caller — and `employee` may only narrow that set.

	`worker_id` IS NOT A PARAMETER, and `employee` is the one that is. The tool's
	own spelling is left undeclared on purpose: `routes.bind` reduces a body to
	the keys a signature names, so a handset that sent `worker_id` would have it
	dropped and would get the whole crew — which is the one direction this filter
	fails safely in, and the refusal below makes the other direction loud.

	A FOREMAN WITH NO OPEN SHIFT GETS THEIR OWN BOARD AND A NOTE SAYING SO,
	rather than an empty answer or an unscoped one. The crew clock is what
	populates this — somebody has to have started a shift — and a dashboard that
	silently showed nothing on a morning before roll call would read as "no work
	today".

	IT COSTS ONE READ PER CREW MEMBER, capped at `CREW_BOARD_CAP`. The alternative
	is a second implementation of the board query here, which is the copy of the
	dispatch rules `api/mobile.py` refuses everywhere else — the claim ceiling,
	the terminal states and the assignment-to-task join all live in
	`tools/dispatch.py` and get to stay there.
	"""
	guard.require_dispatch_role(user, "Reading a crew's dispatch board")
	allowed = guard.require_scope(user)
	wanted = guard.require_company(user, company, allowed)

	named_shift, _label = _one_spelling(shift, farm_shift, "shift", "farm_shift")
	if named_shift:
		named_shift = guard.require_scoped_doc(FARM_SHIFT, named_shift, "shift", allowed)

	shift_rows, crew = _crew_under(user, allowed, wanted, named_shift)
	if named_shift and not shift_rows:
		frappe.throw(
			f"{named_shift} is not a shift you have open. This board answers for the crew on your "
			"own shifts — another foreman's crew is a Desk question, and list_dispatch_board is the "
			"tool that answers it. Nothing was read.",
			frappe.PermissionError,
		)

	wanted_person = str(employee or "").strip()
	if wanted_person:
		known = {entry["employee"] for entry in crew}
		if wanted_person not in known:
			frappe.throw(
				f"{wanted_person} is not on the crew of any shift you have open, so this board does "
				"not answer for them. Roster them with add_worker_to_shift, or read the whole board "
				"in the Desk. Nothing was read.",
				frappe.PermissionError,
			)
		crew = [entry for entry in crew if entry["employee"] == wanted_person]

	total, boards = 0, []
	for entry in crew:
		inner = {"worker_id": entry["employee"], "limit": CREW_BOARD_CAP}
		if wanted:
			inner["company"] = wanted
		if state is not None:
			inner["state"] = state
		if include_finished is not None:
			inner["include_finished"] = include_finished

		data = dispatch.list_dispatched_tasks(inner).data
		rows = []
		for assignment in data.get("assignments") or []:
			detail = assignment.get("task_detail")
			if detail:
				rows.append(shape.task(detail, assignment))
		rows = guard.scoped(rows, allowed)
		total += len(rows)
		boards.append(
			{
				**entry,
				"tasks": rows,
				"count": len(rows),
				"holding_now": data.get("holding_now"),
				"claims_remaining": data.get("claims_remaining"),
			}
		)

	answer = {
		"shifts": [str(row.get("name")) for row in shift_rows],
		"company": wanted or None,
		"crew": boards,
		"crew_size": len(boards),
		"count": total,
	}
	if not shift_rows:
		answer["note"] = (
			"You have no open shift, so this is your own board and nobody else's. Start one with "
			"start_shift and roster the crew with add_worker_to_shift, and everybody on it appears "
			"here."
		)
	return answer


# ── 68. assign_farm_task ─────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("assign_farm_task", mutating=True, limit=guard.WRITE_LIMIT)
def assign_farm_task(
	user: str,
	task=None,
	assigned_to=None,
	employee=None,
	reassign=None,
	reason=None,
	shift=None,
	farm_shift=None,
	override_phi=None,
	phi_override_reason=None,
) -> dict:
	"""Send one named person to one task. v0.72.0.

	THE WIDEST WRITE ON THIS SURFACE, and the only one whose effect lands on
	somebody who is not the caller. Everything else a phone can do it does to its
	own work or to a record it is filing; this takes a job off one person and
	gives it to another. Three things stand in front of it:

	  * `guard.require_dispatch_role` — Foreman or Farm Manager. See the block
	    above this set.
	  * `guard.require_scoped_doc` on the task and `_employee_argument` on the
	    person, so neither may name anything outside the caller's own entities,
	    and something that is outside reads as not found rather than as refused.
	  * `reassign` and `reason`, WHICH THE TOOL ENFORCES AND THIS FORWARDS. Taking
	    work off somebody already holding it is refused unless the body says
	    `reassign=true` AND carries a reason, which is written onto the assignment
	    being closed. That rule is `dispatch.assign_farm_task`'s and it stays
	    there — restating it here would be a second copy of a refusal an auditor
	    reads off one record.

	    IT IS NOT RESTATED EVEN THOUGH `reject_task` RESTATES ITS OWN. The
	    difference is that this refusal is CONDITIONAL: `reassign` means nothing
	    on a task nobody holds, and a wrapper demanding a reason for dispatching
	    unclaimed work would refuse the ordinary case to guard the rare one.

	`assigned_to_name` IS NOT ACCEPTED. The tools write it onto both records in
	place of the name the Employee register holds, and a dispatch record that can
	be made to name somebody who was never sent is not a dispatch record.

	`override_phi` AND `phi_override_reason` ARE FORWARDED, and this is the one
	place on this surface where a phone may set aside a compliance guard. The
	tool refuses a Harvest task on a block still inside its pre-harvest interval
	— see `dispatch._refuse_harvest_inside_phi` for why that one refuses where a
	live restricted-entry interval only warns. The override is here rather than
	MCP-only because the person who knows the stamped date is wrong is standing
	on the block: a window opened by a tank that covered half of it, or a label
	corrected since. `require_dispatch_role` already put a Foreman or a Farm
	Manager on the other end, the reason is mandatory and lands in the task's own
	notes, and the worker's own door — `claim_task_via_mobile` — has no override
	at all.
	"""
	guard.require_dispatch_role(user, "Dispatching a task to somebody")
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_TASK, task, "task", allowed)

	person, label = _one_spelling(assigned_to, employee, "assigned_to", "employee")
	if not person:
		frappe.throw(
			"assigned_to is required — the Employee being sent. A dispatch with no name on it "
			"answers none of the questions it exists to answer. Nothing was changed.",
			frappe.ValidationError,
		)
	person = _employee_argument(person, allowed, label)

	inner = {"task": name, "assigned_to": person}
	if reassign is not None:
		inner["reassign"] = reassign
	if reason is not None:
		inner["reason"] = reason
	if override_phi is not None:
		inner["override_phi"] = override_phi
	if phi_override_reason is not None:
		inner["phi_override_reason"] = phi_override_reason

	named_shift, shift_label = _one_spelling(farm_shift, shift, "farm_shift", "shift")
	if named_shift:
		inner["farm_shift"] = guard.require_scoped_doc(FARM_SHIFT, named_shift, shift_label, allowed)

	result = dispatch.assign_farm_task(inner)
	data = result.data
	out = shape.task(data, data.get("assignment") or {})
	out["reassigned_from"] = data.get("reassigned_from")
	out["concurrent_claims"] = data.get("concurrent_claims")
	if data.get("phi_override"):
		out["phi_override"] = data["phi_override"]
	if data.get("warnings"):
		out["warnings"] = data["warnings"]
	return out


# ── 69. create_farm_task ─────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_farm_task", mutating=True, limit=guard.WRITE_LIMIT)
def create_farm_task(
	user: str,
	task_name=None,
	task_type=None,
	evidence_required=None,
	urgency=None,
	dispatch_mode=None,
	company=None,
	location_doctype=None,
	location=None,
	skill_required=None,
	estimated_duration_minutes=None,
	notes=None,
	assigned_to=None,
	employee=None,
	shift=None,
	farm_shift=None,
	override_phi=None,
	phi_override_reason=None,
	affected_asset=None,
	asset=None,
	affected_block=None,
	observed_at=None,
	reported_by=None,
) -> dict:
	"""Raise one piece of work on the spot, with its evidence contract. v0.72.0.

	THE FIVE STRUCTURED ARGUMENTS ARE v0.98.0, ITEM 5. `SERVER_CHANGES.md` §5:
	everything below the first line of the handset's description is prose the app
	composes and the server keeps as one blob, so the affected asset, the
	affected block, when it was seen and who saw it were greppable and not
	queryable. Each lands in a column — `asset`, the location pair, `observed_at`
	and `reported_by` — and only `observed_at` is new.

	`reported_by` IS ACCEPTED HERE AND REFUSED ON `report_field_task`, and the
	difference is who is speaking. This door is a foreman raising work about
	something a picker told them at the tailgate, and recording whose observation
	it was is the point of the field. There the reporter IS the caller, and a
	body that could name somebody else would put a stranger's name on a report
	they never made.

	`report_field_task` IS THE OTHER DOOR ONTO THIS DOCTYPE AND IS NOT THIS ONE.
	A worker reports a problem and the server decides the shape of the work; a
	foreman raising a task decides it themselves — the type, the urgency, the
	skill, and above all what closing it obliges somebody to produce. Both stay:
	the field report is open to every enrolled worker and rate-limited against
	alarm inflation, and this is Foreman-and-above with the whole form in the
	body.

	`evidence_required` IS MANDATORY AND IS THE POINT. `tools/dispatch.py` refuses
	without it and the refusal names the argument; it is not defaulted here,
	because a wrapper quietly supplying "a photograph will do" would put a
	contract nobody chose onto a compliance record.

	The five arguments this does not accept — `creates_record`,
	`creates_record_data`, `draft`, `source_alert` and `materials_used` — are set
	out in the block that opens this set. Work that has to produce a compliance
	record comes off a template.

	`override_phi` AND `phi_override_reason` ARE THE TWO EXCEPTIONS TO THAT LIST,
	and they are here for the same reason they are on `assign_farm_task`: the tool
	refuses a Harvest task on a block still inside its pre-harvest interval, and
	the person who knows the stamped date is wrong is standing on the block. The
	reason is mandatory and lands in the task's own notes.
	"""
	guard.require_dispatch_role(user, "Raising a farm task")
	allowed = guard.require_scope(user)
	entity = _company(user, company, allowed)

	inner = {"company": entity}
	for key, value in (
		("task_name", task_name),
		("task_type", task_type),
		("evidence_required", evidence_required),
		("urgency", urgency),
		("dispatch_mode", dispatch_mode),
		("location_doctype", location_doctype),
		("location", location),
		("skill_required", skill_required),
		("estimated_duration_minutes", estimated_duration_minutes),
		("notes", notes),
	):
		if value is not None:
			inner[key] = value

	person, label = _one_spelling(assigned_to, employee, "assigned_to", "employee")
	if person:
		inner["assigned_to"] = _employee_argument(person, allowed, label)

	named_shift, shift_label = _one_spelling(farm_shift, shift, "farm_shift", "shift")
	if named_shift:
		inner["farm_shift"] = guard.require_scoped_doc(FARM_SHIFT, named_shift, shift_label, allowed)

	if override_phi is not None:
		inner["override_phi"] = override_phi
	if phi_override_reason is not None:
		inner["phi_override_reason"] = phi_override_reason

	named_asset, _ = _one_spelling(affected_asset, asset, "affected_asset", "asset")
	if named_asset:
		inner["asset"] = named_asset
	for key, value in (("affected_block", affected_block), ("observed_at", observed_at)):
		if value not in (None, ""):
			inner[key] = str(value).strip()
	# SCOPED LIKE EVERY OTHER EMPLOYEE ARGUMENT HERE. `_employee_argument`
	# refuses a docname outside the caller's own entities, so naming the observer
	# cannot become a way to read whether somebody exists on another farm.
	if reported_by not in (None, ""):
		inner["reported_by"] = _employee_argument(reported_by, allowed, "reported_by")

	result = dispatch.create_farm_task(inner)
	data = result.data
	out = shape.task(data, data.get("assignment") or {})
	if data.get("phi_override"):
		out["phi_override"] = data["phi_override"]
	if data.get("warnings"):
		out["warnings"] = data["warnings"]
	return out


# ── 70. list_farm_task_templates ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_farm_task_templates", limit=guard.READ_LIMIT)
def list_farm_task_templates(
	user: str,
	task_type=None,
	skill_required=None,
	enabled=None,
	regime=None,
	company=None,
	limit=None,
) -> dict:
	"""The standing shapes of work this operation has defined. v0.72.0.

	THE PICKER `create_task_from_template` NEEDS, and gated with it rather than
	one step below it. A template register is not sensitive on its own — it is a
	list of the jobs this farm does — but it exists on this surface to be the
	screen a foreman chooses from before raising work, and a read that answers for
	a screen nobody else can reach may as well have the same gate as the screen.

	SCOPED ON THE WAY OUT RATHER THAN ONLY ON THE WAY IN. `company` narrows the
	query when it is sent, and `guard.scoped` runs on the answer either way — a
	template with no company is a template that belongs to the operation rather
	than to an entity, and `scoped` keeps it for the reason it keeps a task with
	none: it is a data question, not another entity's secret.

	A DISABLED TEMPLATE IS STILL LISTED, which is the tool's own decision and is
	forwarded intact: `enabled_templates` is the set new work may be raised from,
	and the app greys the rest rather than hiding them. Hiding them would make a
	foreman who cannot find last season's job believe it never existed.
	"""
	guard.require_dispatch_role(user, "Reading the farm task template register")
	allowed = guard.require_scope(user)
	wanted = guard.require_company(user, company, allowed)

	# `limit` goes through UNPARSED, because `as_limit` is what parses it and
	# `tasktemplates` already caps the answer at the register's own ceiling. An
	# `int()` here would 500 on a body that sent "twenty" instead of refusing it,
	# and would be a second opinion about a number the tool already has one about.
	inner = {"limit": limit if limit is not None else TEMPLATE_LIST_LIMIT}
	if wanted:
		inner["company"] = wanted
	for key, value in (
		("task_type", task_type),
		("skill_required", skill_required),
		("enabled", enabled),
		("regime", regime),
	):
		if value is not None:
			inner[key] = value

	data = template_tools.list_farm_task_templates(inner).data
	templates = guard.scoped(data.get("templates") or [], allowed)
	live = [entry["name"] for entry in templates if entry.get("enabled")]
	return {
		"templates": templates,
		"count": len(templates),
		"enabled_templates": live,
		"company": wanted or None,
	}


# ── 71. create_task_from_template ────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_task_from_template", mutating=True, limit=guard.WRITE_LIMIT)
def create_task_from_template(
	user: str,
	template=None,
	location_doctype=None,
	location=None,
	task_name=None,
	urgency=None,
	notes=None,
	company=None,
	assigned_to=None,
	employee=None,
) -> dict:
	"""Raise one task from a standing template, pre-filled. v0.72.0.

	THE ROUTE FOR WORK THAT PRODUCES A COMPLIANCE RECORD, which is why
	`create_farm_task` above refuses `creates_record` and this does not need to
	accept it. Everything about the task's shape — the type, the skill, the
	duration, the dispatch mode, the evidence contract, the record it builds and
	its defaults, the instructions and the whole checklist — is COPIED off the
	template at creation, by the template's own code. The phone chooses which
	template and where; it composes none of it.

	THE OVERRIDES ARE THE THREE A FOREMAN ACTUALLY HAS AN OPINION ABOUT: where the
	work is, how urgent this instance is, and a note about this particular cabin.
	`creates_record_data` is not among them for the reason the block above gives —
	it writes fields into the compliance record, which is `record_data` wearing a
	different name.

	THE TEMPLATE IS SCOPE-CHECKED like every other docname here. A template
	belonging to an entity this account cannot reach reads as not found, and one
	belonging to no entity is the operation's own and is reachable — the same
	rule `guard.scoped` applies to the list this picks from.
	"""
	guard.require_dispatch_role(user, "Raising a farm task from a template")
	allowed = guard.require_scope(user)

	name = guard.require_scoped_doc(FARM_TASK_TEMPLATE, template, "template", allowed)
	entity = _company(user, company, allowed)

	inner = {"template": name, "company": entity}
	for key, value in (
		("location_doctype", location_doctype),
		("location", location),
		("task_name", task_name),
		("urgency", urgency),
		("notes", notes),
	):
		if value is not None:
			inner[key] = value

	person, label = _one_spelling(assigned_to, employee, "assigned_to", "employee")
	if person:
		inner["assigned_to"] = _employee_argument(person, allowed, label)

	result = template_tools.create_task_from_template(inner)
	data = result.data
	out = shape.task(data, data.get("assignment") or {})
	out["template"] = data.get("template")
	out["checklist"] = data.get("checklist") or []
	if data.get("warnings"):
		out["warnings"] = data["warnings"]
	return out


# ══════════════════════════════════════════════════════════════════════════════
# v0.98.0 — ITEM 11. THE FOUR REGISTERS A TASK CAN BE ROUTED TO.
#
# THE LARGEST GAP ON `SERVER_CHANGES.md` AND THE ONE THAT DEGRADED EVERY OTHER
# FEATURE. The handset's "Where is it" picker showed one option — "No location"
# — because nothing on this surface listed a place and nothing created one.
# Probed 2026-08-18, both namespaces: `create_field`, `create_irrigation_zone`,
# `create_parcel`, `create_housing_unit`, `create_location`,
# `create_farm_location`, `list_farm_locations`, `list_fields`,
# `list_irrigation_zones` and `list_parcels` were ten 404s. The two published
# reads the app scavenged — `list_available_housing` and
# `list_org_reference_data` — cover cabins and parcels only, which on a farm
# whose work happens in BLOCKS is close to nothing, and so a shift's location
# ended up as the typed note "Test 1" that no task could ever be routed against.
#
# EVERY TOOL BEHIND THESE ROUTES ALREADY EXISTED. `list_fields`,
# `list_irrigation_zones`, `list_parcels`, `list_housing_units` and the four
# creates have been MCP tools since v0.12.0 and earlier, reachable from a Desk
# and from an AI console and from nowhere a foreman stands. This is a transport
# gap, not a feature, which is why it is six routes and no new doctype.
#
# ONE LIST AND FIVE DOORS ONTO ONE WRITE. `create_farm_location` is the
# polymorphic one the plan named; `create_field`, `create_irrigation_zone`,
# `create_parcel` and `create_housing_unit` are the four names
# `LocationRegistryAPI.route(for:)` already prints in its own refusal text and
# already builds requests for. All five run `_create_one_location`, so there is
# ONE gate, ONE argument map and ONE set of refusals — a rename cannot open a
# fifth way in, and the four named doors are not a fifth implementation to keep
# in step.
#
# IT IS NOT THE DISPATCHER THIS TRANSPORT REFUSES. `routes.py` opens by saying
# there is no method-name argument and no forwarding; `create_farm_location`
# takes no method name. It takes a DOCTYPE, matched against a closed four-entry
# table in this module, and calls one of four named functions — exactly what
# `attach_file_to_document` already does with its allowlist of parents, and what
# `_attachment_parent` does with `ATTACHMENT_PARENTS`.
# ══════════════════════════════════════════════════════════════════════════════

#: Most locations one list call returns, across all four registers together.
#: Each register is separately capped by its own tool at `REGISTER_CAP`; this is
#: what a phone gets after they are merged, because a picker with four thousand
#: rows in it is a picker nobody scrolls.
LOCATION_LIMIT = 400

#: What each register calls the things `create_farm_location` is given, and
#: which of them it will refuse without.
#:
#: THE FOUR REGISTERS GENUINELY DISAGREE AND NOTHING HERE PRETENDS OTHERWISE. A
#: block is named `field_name` and hangs off a `parcel`; a zone is named
#: `zone_name` and hangs off a `field`, NOT a parcel, because a zone waters a
#: block; a parcel is named `parcel_name` and hangs off nothing at all, being
#: the top of the tree; a cabin is named `unit_name` and hangs off a `parcel`.
#: The handset collects one `name` and one optional `parcel` for all four, so
#: this table is where that uniform sheet becomes four different documents.
#:
#: `acres` IS THE ODD ONE AND `Irrigation Zone` IS WHY. Three registers take an
#: acreage directly; `create_irrigation_zone` REFUSES `area_acres` by name,
#: because `area_acres` is COMPUTED from `area_sq_ft` by the controller and two
#: independently settable figures are two figures that will disagree. So a zone's
#: acres are converted to square feet here — one figure, still, set once — and
#: `SQ_FT_PER_ACRE` is the exact definition rather than a rounding.
SQ_FT_PER_ACRE = 43560

LOCATION_REGISTERS = {
	"Field": {
		"tool": "create_field",
		"name_argument": "field_name",
		"parent_argument": "parcel",
		"parent_doctype": "Parcel",
		"acres_argument": "acreage",
	},
	"Irrigation Zone": {
		"tool": "create_irrigation_zone",
		"name_argument": "zone_name",
		"parent_argument": "field",
		"parent_doctype": "Field",
		"acres_argument": "area_sq_ft",
		"acres_factor": SQ_FT_PER_ACRE,
	},
	"Parcel": {
		"tool": "create_parcel",
		"name_argument": "parcel_name",
		"parent_argument": None,
		"parent_doctype": None,
		"acres_argument": "acreage",
	},
	"Housing Unit": {
		"tool": "create_housing_unit",
		"name_argument": "unit_name",
		"parent_argument": "parcel",
		"parent_doctype": "Parcel",
		"acres_argument": None,
	},
}


def _scoped_location(doctype: str, name, label: str, allowed: list) -> str:
	"""A location docname that exists AND belongs to an entity the caller may reach.

	`guard.require_scoped_doc` CANNOT DO THIS JOB AND WOULD HAVE PASSED
	EVERYTHING. It reads a column called `company`, and ALL FOUR of these
	registers call theirs `owning_entity` — so the lookup returns None, the
	`if company and …` guard is skipped, and a docname from another farm on the
	same bench is accepted. `_attachment_parent` above hit the identical trap on
	`Housing Unit` and makes the identical hand-made check; this is that check
	for four doctypes rather than one, and the reason it is worth its own
	function is that a Farm Manager on one entity filing a block under another
	entity's parcel is a permanent, unmergeable row in somebody else's register.

	A ROW WITH NO OWNING ENTITY IS REACHABLE, which is the same rule
	`guard.scoped` applies to a task and a template with no company: ground that
	names no entity belongs to the operation rather than to one of its companies,
	and refusing it would make a single-entity farm's own parcels unusable.

	NOT FOUND RATHER THAN REFUSED, so a caller cannot map another entity's
	docnames by watching which error comes back.
	"""
	docname = guard.require_docname(doctype, name, label)
	owner = str(frappe.db.get_value(doctype, docname, "owning_entity") or "")
	if owner and owner not in set(allowed or []):
		frappe.throw(f"{label} {docname} was not found.", frappe.DoesNotExistError)
	return docname


def _location_register(doctype, label: str = "doctype") -> str:
	"""One of the four registers, or a refusal naming all four.

	CASE-INSENSITIVE ON THE WAY IN AND EXACT ON THE WAY OUT, so `field` and
	`Field` both work and what is stored is the doctype's own spelling — the same
	call `args.as_choice` makes about a Select, for the same reason: what is
	written has to match what a filter on the list view will look for.
	"""
	wanted = str(doctype or "").strip()
	if not wanted:
		frappe.throw(
			f"{label} is required. A place without its register is not a place — pass one of: "
			+ ", ".join(locations.REGISTERS)
			+ ". Nothing was created.",
			frappe.ValidationError,
		)
	for register in locations.REGISTERS:
		if register.lower() == wanted.lower():
			return register
	frappe.throw(
		f"{wanted} is not a register this surface creates locations in. The ones it does are: "
		+ ", ".join(locations.REGISTERS)
		+ ". Nothing was created.",
		frappe.PermissionError,
	)


def _location_rows(register: str, entity: str, allowed: list) -> list:
	"""One register's rows, as location options, scoped to the caller.

	EACH REGISTER IS READ THROUGH ITS OWN TOOL rather than off the table, which
	is what keeps the derivations. A Field's county is read through its parcel on
	every call and stored nowhere; a Housing Unit's occupancy is counted from
	live assignments. A second reader going straight to `frappe.db.get_all` would
	have been faster and would have quietly dropped both.

	A REGISTER THIS SITE HAS NOT INSTALLED CONTRIBUTES NOTHING RATHER THAN
	FAILING THE CALL. The four tools each refuse a missing doctype by name, which
	is right on a console and wrong here: a farm with no irrigation zones
	registered should get a picker with three sections in it, not an error.
	"""
	if not compat.doctype_exists(register):
		return []
	inner = {"limit": farm_tools.REGISTER_CAP}
	if entity:
		inner["company"] = entity
	try:
		if register == "Field":
			rows = farm_tools.list_fields(inner).data.get("fields") or []
		elif register == "Irrigation Zone":
			rows = farm_tools.list_irrigation_zones(inner).data.get("zones") or []
		elif register == "Parcel":
			rows = realestate_tools.list_parcels(inner).data.get("parcels") or []
		else:
			rows = housing_tools.list_housing_units(inner).data.get("units") or []
	except ToolError:
		# `list_parcels` REQUIRES a company and the other three do not, so an
		# account with no entity at all reaches this. Three empty sections and a
		# populated fourth is a worse answer than four empty ones, and neither is
		# an error the person holding the phone can act on.
		return []
	return [locations.option(register, dict(row)) for row in rows]


# ── 71b. list_farm_locations ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_farm_locations", limit=guard.READ_LIMIT)
def list_farm_locations(user: str, company=None, doctype=None, register=None, limit=None) -> dict:
	"""Every place this farm can route work to, from all four registers, in one call.

	THE READ THAT MAKES THE PICKER A PICKER. It is deliberately the FIRST thing
	in item 11 and the one that fixes the screen on its own: every farm whose
	blocks already exist gets a populated picker out of this route alone, with no
	create and no new role. `SERVER_CHANGES.md` had it as a "Server TODO" under
	§26.2 before any of the rest of this was written up.

	OPEN ON ENROLMENT ALONE, AND THAT IS DELIBERATE RATHER THAN LEFT OVER. The
	write below is Farm Manager; this is every picker on the crew, because
	`report_field_task` is open to every enrolled worker and TAKES A LOCATION. A
	read gated on the dispatch role would have left the one call a field worker
	makes about a place unable to name one — which is the bug, restated.

	FOUR REGISTERS, ONE ROW SHAPE, AND NEITHER MERGED NOR FLATTENED. Each row
	carries its own docname and its own `doctype`, because `location` without
	`location_doctype` is the refusal all three task-raising tools open with and
	the reason `TaskLocationOption` has a failable initialiser. `location_type`
	is the same string a third time, matching `shape.task`, so a caller decoding
	a task and a caller decoding this list read one key. See
	`erpnext_mcp/locations.py` for the row and for why the zones are grouped by
	parcel rather than by block.

	SCOPED TWICE AND ON PURPOSE. `company` narrows the query on the way in, and
	`guard.scoped` runs on the merged answer either way — the four tools spell
	the owning entity two different ways (`owning_entity` and `company`) and
	`locations.option` reconciles them, so the outbound check has one key to look
	at rather than two to remember.
	"""
	allowed = guard.require_scope(user)
	entity = _company(user, company, allowed)

	wanted, label = _one_spelling(doctype, register, "doctype", "register")
	registers = [_location_register(wanted, label)] if wanted else list(locations.REGISTERS)

	rows = []
	for name in registers:
		rows.extend(_location_rows(name, entity, allowed))
	rows = guard.scoped(rows, allowed)
	rows.sort(key=locations.sort_key)

	cap = LOCATION_LIMIT
	if limit not in (None, ""):
		try:
			cap = max(1, min(int(limit), LOCATION_LIMIT))
		except (TypeError, ValueError):
			frappe.throw(f"limit must be a whole number, got {limit!r}.", frappe.ValidationError)
	truncated = len(rows) > cap

	by_register = {name: 0 for name in locations.REGISTERS}
	for row in rows[:cap]:
		by_register[row["doctype"]] = by_register.get(row["doctype"], 0) + 1

	return {
		"company": entity or None,
		"locations": rows[:cap],
		"count": len(rows[:cap]),
		"total": len(rows),
		"truncated": truncated,
		"by_register": by_register,
		# What the picker draws its sections from, INCLUDING the empty ones. A
		# farm with no irrigation zones registered should see the section and
		# learn that zones are a thing this app has, rather than see three
		# sections and conclude the fourth does not exist.
		"registers": [name for name in locations.REGISTERS if compat.doctype_exists(name)],
		# Whether the caller may add to them. The handset gates its "Add a place"
		# button on its own role list, calls that a courtesy, and asks for the
		# real answer — this is the real answer, from the same function the write
		# below actually uses, so the button and the door cannot disagree.
		"can_create": bool(guard.roles_held(user) & guard.LOCATION_ROLES),
		"create_roles": sorted(guard.LOCATION_ROLES),
	}


def _create_one_location(user: str, register: str, arguments: dict) -> dict:
	"""The one write behind all five create routes. Gate, map, delegate.

	ONE IMPLEMENTATION SO A RENAME CANNOT OPEN A FIFTH WAY IN. The four named
	routes exist because `LocationRegistryAPI.route(for:)` already names them and
	the app already builds their requests; they are spellings of this, not
	copies of it, which is the same call v0.62.0 made for the housing pair and
	v0.57.0 made for `submit_form_signature`.

	THE GATE RUNS BEFORE ANYTHING IS READ, and it is `require_location_role`
	rather than `require_dispatch_role` — see `guard.LOCATION_ROLES` for why
	adding a place is harder than sending somebody to one, and for the answer to
	§11's question about which role name is real.

	THE PARENT IS SCOPE-CHECKED, NOT MERELY EXISTENCE-CHECKED. A parcel belonging
	to an entity this account cannot reach reads as not found — the same refusal
	`require_scoped_doc` gives everywhere else — so this cannot become a way to
	discover which parcels another farm on the same bench has by watching which
	error comes back.
	"""
	guard.require_location_role(user, f"Adding a {register} to the location register")
	allowed = guard.require_scope(user)
	spec = LOCATION_REGISTERS[register]

	given = str(arguments.get("name") or arguments.get(spec["name_argument"]) or "").strip()
	if not given:
		frappe.throw(
			f"name is required — a {register} with no name is a docname nobody can pick from a "
			f"list. This register calls it {spec['name_argument']}; either spelling is accepted. "
			"Nothing was created.",
			frappe.ValidationError,
		)

	entity = guard.require_company(user, arguments.get("company"), allowed) or (allowed[0] if allowed else "")
	inner = {spec["name_argument"]: given}
	if entity:
		inner["company"] = entity

	parent_argument = spec["parent_argument"]
	if parent_argument:
		parent = str(arguments.get(parent_argument) or arguments.get("parent") or "").strip()
		if not parent and parent_argument != "parcel":
			# A ZONE'S PARENT IS A FIELD AND THE HANDSET SENDS A PARCEL. Named in
			# the refusal rather than guessed at: a parcel usually holds several
			# blocks and picking one of them for somebody would put a zone on
			# ground it does not water.
			parent = str(arguments.get("parcel") or "").strip()
			if parent:
				frappe.throw(
					f"a {register} hangs off a {spec['parent_doctype']}, not a Parcel — it waters "
					"one block rather than a whole title. Send "
					f"{parent_argument}=<{spec['parent_doctype']} docname>; list_farm_locations "
					f"has the {spec['parent_doctype']} register. Nothing was created.",
					frappe.ValidationError,
				)
		if not parent:
			frappe.throw(
				f"{parent_argument} is required — a {register} is filed under a "
				f"{spec['parent_doctype']} and its docname is built from it, so one created "
				f"without a {spec['parent_doctype']} would have nowhere to live. "
				"list_farm_locations has the register. Nothing was created.",
				frappe.ValidationError,
			)
		inner[parent_argument] = _scoped_location(spec["parent_doctype"], parent, parent_argument, allowed)

	acres = arguments.get("acres")
	if acres in (None, ""):
		acres = arguments.get(spec["acres_argument"] or "") if spec["acres_argument"] else None
	if acres not in (None, "") and spec["acres_argument"]:
		try:
			measured = float(acres)
		except (TypeError, ValueError):
			frappe.throw(
				f"acres must be a number, got {acres!r}. Nothing was created.", frappe.ValidationError
			)
		if measured < 0:
			frappe.throw(
				f"acres must be zero or more, got {measured}. Nothing was created.",
				frappe.ValidationError,
			)
		inner[spec["acres_argument"]] = round(measured * spec.get("acres_factor", 1), 4)

	for key in ("notes", "unit_type", "capacity", "crop", "variety", "block_number", "county", "state"):
		value = arguments.get(key)
		if value not in (None, ""):
			inner[key] = value

	result = getattr(_LOCATION_TOOLS[register], spec["tool"])(inner)
	data = dict(result.data)
	# THE ANSWER IS A LOCATION OPTION AS WELL AS THE RECORD. The screen that
	# posted this is a picker, and the next thing it does is select what it just
	# made — so it is handed the pair it will send back as `location_doctype` and
	# `location`, rather than being left to work out which of the register's
	# fourteen keys is the docname.
	return {
		**data,
		"doctype": register,
		"location_type": register,
		"location": data.get("name"),
		"option": locations.option(register, data),
	}


#: Which module holds each register's create tool. A table rather than four
#: imports at each call site, so the four named routes below stay one line each.
_LOCATION_TOOLS = {
	"Field": farm_tools,
	"Irrigation Zone": farm_tools,
	"Parcel": realestate_tools,
	"Housing Unit": housing_tools,
}


# ── 71c. create_farm_location ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_farm_location", mutating=True, limit=guard.WRITE_LIMIT)
def create_farm_location(
	user: str,
	name=None,
	doctype=None,
	register=None,
	company=None,
	parcel=None,
	field=None,
	parent=None,
	acres=None,
	notes=None,
	unit_type=None,
	capacity=None,
	crop=None,
	variety=None,
	block_number=None,
	county=None,
	state=None,
) -> dict:
	"""Add one place to one of the four registers. The polymorphic door.

	THE SHEET THE HANDSET ACTUALLY COLLECTS. `CreateLocationSheet` asks for a
	name, a register and — where the register has a parent — a parcel, plus
	acres; `LocationRegistryAPI.create` builds exactly `{name, company, parcel,
	acres}` and then throws before touching the network. This takes that body
	unchanged, which is why it is one method with a `doctype` rather than four
	with four different argument sets.

	THE FOUR NAMED ROUTES BELOW ARE THE SAME WRITE and exist because the app
	already prints their names in its own refusal. Nothing is duplicated: all
	five call `_create_one_location`, where the gate and the argument map live.

	`doctype` IS NOT A METHOD NAME AND THIS IS NOT THE DISPATCHER `routes.py`
	REFUSES. It is matched against `LOCATION_REGISTERS`, a closed four-entry
	table in this file, and resolves to one of four named functions — the same
	shape `attach_file_to_document` has had since v0.78.0.

	THE OPTIONAL ARGUMENTS ARE THE ONES A PERSON AT A TAILGATE HAS AN OPINION
	ABOUT: what it is called, where it hangs, how big it is, what is planted on
	it, how many it sleeps, and a note. EVERYTHING ELSE THE FOUR TOOLS ACCEPT IS
	ABSENT FROM THIS SIGNATURE and therefore unreachable, because `routes.bind`
	delivers only what is declared — `title_holder`, `appraised_value`,
	`related_asset`, `external_farm_app_id`, `water_right_id`, the organic
	certification block and the boundary geometry are all desk work with a
	document open, and several of them are the kind of number that ends up in a
	financial statement.
	"""
	return _create_one_location(
		user,
		_location_register(*_one_spelling(doctype, register, "doctype", "register")),
		{
			"name": name,
			"company": company,
			"parcel": parcel,
			"field": field,
			"parent": parent,
			"acres": acres,
			"notes": notes,
			"unit_type": unit_type,
			"capacity": capacity,
			"crop": crop,
			"variety": variety,
			"block_number": block_number,
			"county": county,
			"state": state,
		},
	)


# ── 71d. create_field ────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_field", mutating=True, limit=guard.WRITE_LIMIT)
def create_field(
	user: str,
	name=None,
	field_name=None,
	parcel=None,
	company=None,
	acres=None,
	acreage=None,
	crop=None,
	variety=None,
	block_number=None,
	notes=None,
) -> dict:
	"""Register one planted block under a parcel. `LocationRegistryAPI`'s own name.

	A SPELLING OF `create_farm_location`, NOT A SECOND IMPLEMENTATION — see the
	block that opens this set. `field_name` is the register's word and `name` is
	the handset's; `acreage` is the register's and `acres` is the handset's. Both
	of each, because `routes.bind` drops what a signature does not name and a
	method that took one of them would be a silent empty column for whichever
	caller guessed wrong.
	"""
	return _create_one_location(
		user,
		"Field",
		{
			"name": name or field_name,
			"parcel": parcel,
			"company": company,
			"acres": acres if acres not in (None, "") else acreage,
			"crop": crop,
			"variety": variety,
			"block_number": block_number,
			"notes": notes,
		},
	)


# ── 71e. create_irrigation_zone ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_irrigation_zone", mutating=True, limit=guard.WRITE_LIMIT)
def create_irrigation_zone(
	user: str,
	name=None,
	zone_name=None,
	field=None,
	parcel=None,
	company=None,
	acres=None,
	area_sq_ft=None,
	notes=None,
) -> dict:
	"""Register one irrigation zone under a block.

	`field` AND NOT `parcel`, WHICH IS THE ONE PLACE THE FOUR REGISTERS DIVERGE
	IN A WAY THE HANDSET'S UNIFORM SHEET CANNOT SEE. A zone waters a block, not a
	whole title; `parcel` is declared here anyway so that a body carrying it gets
	the sentence explaining the difference rather than "field is required" about
	an argument it never heard of.

	`acres` BECOMES SQUARE FEET ON THE WAY IN. `create_irrigation_zone` refuses
	`area_acres` by name — the controller computes it from `area_sq_ft`, and two
	independently settable figures are two figures that will disagree — so this
	converts rather than sets a second one. `area_sq_ft` is accepted directly for
	a caller who measured it that way.
	"""
	return _create_one_location(
		user,
		"Irrigation Zone",
		{
			"name": name or zone_name,
			"field": field,
			"parcel": parcel,
			"company": company,
			# The converted figure goes in as `acres`; a caller who sent square
			# feet outright has already done the conversion, so it is passed
			# under the register's own key and skips the factor.
			"acres": acres,
			"area_sq_ft": area_sq_ft,
			"notes": notes,
		},
	)


# ── 71f. create_parcel ───────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_parcel", mutating=True, limit=guard.WRITE_LIMIT)
def create_parcel(
	user: str,
	name=None,
	parcel_name=None,
	company=None,
	acres=None,
	acreage=None,
	county=None,
	state=None,
	notes=None,
) -> dict:
	"""Register one parcel: the title the rest of the ground hangs off.

	NO PARENT, because a parcel is the top of the tree — which is why
	`LOCATION_REGISTERS["Parcel"]["parent_argument"]` is None and why a picker
	draws it as a heading rather than as a child of anything.

	`title_holder`, `appraised_value`, `appraiser` AND `appraisal_document` ARE
	ABSENT FROM THIS SIGNATURE, so `bind` drops them. What a piece of ground is
	worth and who holds the deed are figures that reach a financial statement,
	and they are settled at a desk with the paperwork open — `update_parcel` on
	the MCP surface is where they live.
	"""
	return _create_one_location(
		user,
		"Parcel",
		{
			"name": name or parcel_name,
			"company": company,
			"acres": acres if acres not in (None, "") else acreage,
			"county": county,
			"state": state,
			"notes": notes,
		},
	)


# ── 71g. create_housing_unit ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_housing_unit", mutating=True, limit=guard.WRITE_LIMIT)
def create_housing_unit(
	user: str,
	name=None,
	unit_name=None,
	parcel=None,
	company=None,
	unit_type=None,
	capacity=None,
	notes=None,
) -> dict:
	"""Register one building on a camp.

	NO ACREAGE — a cabin is measured in beds and square feet, not acres, and
	`LOCATION_REGISTERS["Housing Unit"]["acres_argument"]` is None so an `acres`
	sent by the handset's uniform sheet is ignored rather than written somewhere
	it would be wrong.

	THE HABITABILITY BLOCK IS DELIBERATELY ABSENT. `or_housing_law_compliant`,
	the two detector test dates and `last_habitability_inspection` are what an
	inspection FINDS, and a route that let them be asserted at creation would let
	a cabin be registered as already compliant by whoever registered it.
	`create_housing_inspection` is how those columns move.
	"""
	return _create_one_location(
		user,
		"Housing Unit",
		{
			"name": name or unit_name,
			"parcel": parcel,
			"company": company,
			"unit_type": unit_type,
			"capacity": capacity,
			"notes": notes,
		},
	)


#: Which tool sets which register's shape, and what that tool calls the record.
#:
#: THE SAME THREE `api/gis.SAVEABLE` NAMES, REACHED THE SAME WAY. That table is
#: the Desk map's allowlist and this one is the phone's; both resolve a closed
#: set of registers to the three tools the AI also calls, and neither takes a
#: method name from a caller. Two tables rather than one import because the two
#: transports hold DIFFERENT things: `SAVEABLE` carries the callables because its
#: gate is `frappe.has_permission` on a Desk session, and this one carries the
#: module and the tool NAME because `test_wave2_mobile_surface` reads the source
#: of the wrapper to prove which gate ran.
BOUNDARY_REGISTERS = {
	"Field": {"module": farm_tools, "tool": "set_field_boundary", "argument": "field"},
	"Irrigation Zone": {"module": farm_tools, "tool": "set_zone_boundary", "argument": "zone"},
	"Parcel": {"module": realestate_tools, "tool": "set_parcel_boundary", "argument": "parcel"},
}


def _set_one_boundary(user: str, register: str, name, geojson, dry_run) -> dict:
	"""The one write behind all three boundary routes. Gate, scope, delegate.

	v0.110.0, and it REVERSES A SENTENCE THIS SURFACE HAS BEEN CARRYING SINCE
	v0.98.0: "`set_field_boundary`, `set_zone_boundary`, `set_parcel_boundary` …
	are DELIBERATELY ABSENT. Drawing a boundary … is a desk act with a document
	open." That was true of drawing one with a mouse on satellite imagery, which
	is the only way it could be done when it was written.

	IT IS NOT TRUE OF WALKING ONE. A boundary recorded by carrying a phone round
	the edge of a block is a ring of GPS fixes, and the person holding the phone
	is standing on the corner rather than guessing at it from an image taken in a
	different season. That is a BETTER boundary than a traced one for exactly the
	ground this app is about: an orchard block's corner is a change in canopy, and
	the difference between where the canopy looks like it ends and where the last
	row actually is comes to acres over a farm.

	EVERY CHECK THE DESK MAP GETS, THIS GETS, because it is the same three tools.
	The polygon is parsed, a self-intersection is refused, the enclosed area is
	compared against the recorded acreage and a disagreement past a quarter is
	REFUSED outright, containment against the shape above and below is reported,
	and every derived field — centroid, bounding box, H3 coverage, computed acres
	— is recomputed from the polygon rather than typed. Nothing is reimplemented
	here and no check is relaxed for a phone.

	THE AREA CHECK IS WHAT MAKES A WALKED BOUNDARY SAFE, and it is worth saying
	which failure it catches: a walk that cut a corner, stopped early, or was
	recorded with the phone in a pocket losing fixes produces a polygon that is
	perfectly valid, is on Earth, and encloses noticeably less ground than the
	block is recorded as. That is refused with both figures named. A walk that
	came out within a few percent is a walk that agrees with the deed.

	THE GATE IS `require_location_role` — Farm Manager — WHICH IS THE SAME GATE
	AS THE CREATES ABOVE AND NOT AN ACCIDENT OF COPYING. A register entry is
	permanent in a way a task is not, and a boundary is the more consequential
	half of it: every geofence answer, every "was the crew in an authorised
	area", every acre of cost allocation and every Worker Protection Standard
	answer about which block was sprayed resolves through this polygon. A
	plausible-but-wrong shape passes every validation the tool makes. The refusal
	names the alternative, as `guard.require_location_role` always does: the walk
	can be recorded and somebody at a desk can apply it.

	THE OWNING ENTITY IS READ OFF THE RECORD AND NEVER TAKEN FROM THE BODY. The
	three tools resolve a company when they are not given one, and on a
	multi-entity site that is a refusal rather than a guess — so the entity is
	read from the document the caller already proved they may reach, which is the
	same call `api/gis._save_boundary` makes for the Desk. A phone that could name
	the entity could file a shape against a register it was not scoped to.

	`dry_run` GOES STRAIGHT THROUGH, because the tools already have it and a
	handset has the most obvious use for it in the app: the walk is finished, the
	operator is still standing in the block, and "what would this shape do" before
	"do it" is the difference between a correction that takes thirty seconds and
	one that takes a drive back out.
	"""
	guard.require_location_role(user, f"Setting the boundary of a {register}")
	allowed = guard.require_scope(user)
	spec = BOUNDARY_REGISTERS[register]

	# `guard.require_scoped_doc` CANNOT DO THIS JOB AND WOULD PASS EVERYTHING —
	# it reads a column called `company` and all three of these registers call
	# theirs `owning_entity`. `_scoped_location` is the hand-made check that
	# exists for exactly that trap, and it answers NOT FOUND rather than refused
	# so a caller cannot map another entity's docnames by watching which error
	# comes back.
	docname = _scoped_location(register, name, spec["argument"], allowed)

	shape = geojson
	if shape in (None, ""):
		frappe.throw(
			f"boundary_geojson is required — a {register} boundary with no polygon in it is not "
			"a boundary. Send the walk as a GeoJSON Polygon or MultiPolygon in [longitude, "
			"latitude] degrees. Nothing was changed.",
			frappe.ValidationError,
		)
	if not isinstance(shape, str):
		# `frappe.call` posts a JS object as JSON and Frappe hands some bodies
		# back already decoded, so the polygon arrives as a dict about as often as
		# it arrives as a string. `geo.parse` reads a string; re-encoding here is
		# one line and saves a refusal that would read as "your polygon is
		# invalid" about a polygon that is perfectly fine.
		shape = json.dumps(shape)

	inner = {spec["argument"]: docname, "boundary_geojson": shape}
	owner = str(frappe.db.get_value(register, docname, "owning_entity") or "")
	if owner:
		inner["owning_entity"] = owner
	if dry_run not in (None, ""):
		inner["dry_run"] = 1 if str(dry_run).strip().lower() in ("1", "true", "yes") else 0

	result = getattr(spec["module"], spec["tool"])(inner)
	data = dict(result.data)
	# THE ANSWER CARRIES THE PAIR THE HANDSET SENDS BACK, the same shape
	# `_create_one_location` returns and for the same reason: the screen that
	# posted this walk is a location screen, and the next thing it does is name
	# the place it just measured.
	return {
		**data,
		"doctype": register,
		"location_type": register,
		"location": docname,
	}


# ── 71h. set_field_boundary ──────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("set_field_boundary", mutating=True, limit=guard.WRITE_LIMIT)
def set_field_boundary(user: str, field=None, name=None, boundary_geojson=None, dry_run=None) -> dict:
	"""Record the shape of one planted block, from a walk round its edge.

	`field` IS THE REGISTER'S WORD AND `name` IS THE HANDSET'S, both accepted for
	the same reason `create_field` takes both: `routes.bind` drops what a
	signature does not name, and a method that took one of them would 404 the
	argument for whichever caller guessed the other.

	`owning_entity` AND `company` ARE ABSENT FROM THIS SIGNATURE, so `bind` drops
	them and no body can file a polygon against an entity this account is not
	scoped to. The entity is read off the block itself — see `_set_one_boundary`.
	"""
	return _set_one_boundary(user, "Field", field or name, boundary_geojson, dry_run)


# ── 71i. set_zone_boundary ───────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("set_zone_boundary", mutating=True, limit=guard.WRITE_LIMIT)
def set_zone_boundary(user: str, zone=None, name=None, boundary_geojson=None, dry_run=None) -> dict:
	"""Record the shape of one irrigation zone.

	THE ONE THAT REPORTS RATHER THAN REFUSES. `set_zone_boundary` answers whether
	the zone sits inside the block it waters and never enforces it, because a
	shared water line crosses a boundary, a pump house sits on the headland and a
	mainline runs down a road easement. `boundary_contained_in_field` comes back
	true, false, or null when the block has no boundary of its own to check
	against — and null is a different answer from false, which is why the walk
	that recorded the block should be the one done first.
	"""
	return _set_one_boundary(user, "Irrigation Zone", zone or name, boundary_geojson, dry_run)


# ── 71j. set_parcel_boundary ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("set_parcel_boundary", mutating=True, limit=guard.WRITE_LIMIT)
def set_parcel_boundary(user: str, parcel=None, name=None, boundary_geojson=None, dry_run=None) -> dict:
	"""Record the shape of one parcel: the outer line the deed describes.

	THE ONE A PHONE IS LEAST OFTEN THE RIGHT INSTRUMENT FOR, and it is published
	anyway rather than withheld. A parcel line is what an assessor surveyed, and
	`api/gis.query_county_parcels` imports Wasco County's own polygon on the Desk
	— which is a better source than anybody walking a fence. But a farm outside
	Wasco County has no such import, several deed lines on any farm run down the
	middle of a creek nobody can survey by eye off imagery, and a walked outline
	that agrees with the deeded acreage to a few percent is a great deal better
	than no outline at all. The tool's own area check is what decides which of
	those a given walk was.

	SETTING IT REPORTS WHAT NOW FALLS OUTSIDE — every block, zone and cabin
	registered on the parcel that has a position and is no longer inside it. That
	is the answer worth having on the phone rather than in an email later, since
	the person holding it is standing on the line in question.
	"""
	return _set_one_boundary(user, "Parcel", parcel or name, boundary_geojson, dry_run)


# ── 72. list_cost_centers ───────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_cost_centers", limit=guard.READ_LIMIT)
def list_cost_centers(user: str, company=None, include_disabled=None) -> dict:
	"""The cost center tree for one entity, so a receipt has something to be coded to.

	`create_expense_receipt` and `update_expense_receipt` both take a
	`cost_center` docname; until this route existed, the only way a phone
	learned a valid one was a bookkeeper reciting it. `tools/dimensions.py`'s
	`list_cost_centers` REQUIRES a company, so this falls back to the caller's
	first entity the same way `create_expense_receipt` does, rather than
	refusing a call that named none.
	"""
	allowed = guard.require_scope(user)
	wanted = _company(user, company, allowed)

	inner = {"company": wanted}
	if include_disabled is not None:
		inner["include_disabled"] = include_disabled

	return dimension_tools.list_cost_centers(inner).data


# ── 73. list_suppliers ──────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_suppliers", limit=guard.READ_LIMIT)
def list_suppliers(user: str, company=None, supplier_group=None, search=None, limit=None) -> dict:
	"""Suppliers by group and name, for the picker `create_expense_receipt` feeds.

	Supplier IS A SHARED REGISTER, not a per-company one — the doctype carries no
	company field of its own, so `company` only narrows the answer where the
	underlying tool's own company-scoping note says it can. `guard.scoped` still
	runs on the way out, the same belt every list on this surface wears.
	"""
	allowed = guard.require_scope(user)
	wanted = guard.require_company(user, company, allowed)

	inner = {}
	for key, value in (
		("company", wanted),
		("supplier_group", supplier_group),
		("search", search),
		("limit", limit),
	):
		if value not in (None, ""):
			inner[key] = value

	data = master_tools.list_suppliers(inner).data
	rows = guard.scoped(data.get("suppliers") or [], allowed)
	return {
		"suppliers": rows,
		"count": len(rows),
		"company": wanted or None,
	}


# ── 74. list_expense_receipts ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_expense_receipts", limit=guard.READ_LIMIT)
def list_expense_receipts(user: str, company=None, status=None, limit=None) -> dict:
	"""Receipts already captured, for the detail view `create_expense_receipt` feeds into.

	SCOPED TWICE, like every other list here. The tool filters by company when
	one is sent; `guard.scoped` runs on the answer either way, because a row
	that escapes the filter through a code path nobody thought about is the
	failure this surface exists to prevent.
	"""
	allowed = guard.require_scope(user)
	wanted = guard.require_company(user, company, allowed)

	inner = {}
	for key, value in (
		("company", wanted),
		("status", status),
		("limit", limit),
	):
		if value not in (None, ""):
			inner[key] = value

	data = expense_tools.list_expense_receipts(inner).data
	rows = guard.scoped(data.get("receipts") or [], allowed)
	return {
		"receipts": rows,
		"count": len(rows),
		"company": wanted or None,
		"total_amount": round(sum(float(row.get("amount") or 0) for row in rows), 2),
	}


# ── 75. update_expense_receipt ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("update_expense_receipt", mutating=True, limit=guard.WRITE_LIMIT)
def update_expense_receipt(
	user: str,
	receipt_name=None,
	category=None,
	supplier=None,
	cost_center=None,
	notes=None,
) -> dict:
	"""Recode a captured receipt's cost center, supplier, category or notes.

	ONLY THE FOUR FIELDS THE TOOL HONORS ARE ON THIS SIGNATURE. The underlying
	`tools/expenses.py::update_expense_receipt` refuses to touch `merchant`,
	`amount`, `receipt_date` or `status` — those are the machine's reading of
	the paper and the record of a decision, not something a desk correction
	rewrites — so this wrapper does not offer fields that would silently do
	nothing.

	`receipt_name` IS SCOPED THE SAME WAY `update_regulatory_filing`'s `filing`
	is: `guard.require_scoped_doc` confirms the receipt exists AND belongs to
	an entity the caller may reach, before the tool ever sees it. `_company`
	would have resolved a company to default TO, not verified the receipt
	being edited is actually IN one the caller is allowed to touch.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(EXPENSE_RECEIPT, receipt_name, "receipt_name", allowed)

	inner = {"name": name}
	for key, value in (
		("category", category),
		("supplier", supplier),
		("cost_center", cost_center),
		("notes", notes),
	):
		if value not in (None, ""):
			inner[key] = value

	return expense_tools.update_expense_receipt(inner).data


# ══════════════════════════════════════════════════════════════════════════════
# Sprint 8 (v0.78.0) — field asset registration
#
# THE THREE THE iOS REGISTRATION SCREENS WERE BLOCKED ON. `register_asset` and
# `generate_asset_qr` have existed as MCP tools since v0.25.0 and
# `attach_file_to_document` since v0.11.0, and none of the three had a route —
# so the flow a worker actually performs at a machine (photograph the plate,
# register it, print the tag, file the photograph) stopped at step two with a
# 404. The Swift screens are built and call these names.
#
# THE ARGUMENT SPELLINGS ARE THE TOOLS' OWN, deliberately. `routes.bind` reduces
# a request body to the keys a signature declares, so a wrapper that renamed
# `name` to `asset_name` would silently drop the tag ID off every registration.
# Where a tool takes two spellings for one column — `parent_asset` and
# `location` — both are declared, because both are equally likely to arrive and
# the tool refuses them only when they DISAGREE.
#
# `attach_file_to_document` IS THE ONE THAT IS NARROWED, and the narrowing is
# the point of publishing it at all. The tool will attach a file to any document
# on the site, which on this surface would mean a field worker's handset could
# grow the evidence on a submitted Journal Entry or a signed I-9. So the wrapper
# carries an allowlist of the registers a field app legitimately writes into,
# refuses everything else by name, and does not declare `allow_cancelled` at
# all — a cancelled document is history, and a phone is not where somebody
# decides to add to it.
# ══════════════════════════════════════════════════════════════════════════════

#: Which doctypes a handset may file an attachment against. EVERY ENTRY IS A
#: REGISTER THIS SURFACE ALREADY WRITES TO through some other route — an asset it
#: just registered, a task it is completing, a receipt it photographed, an
#: inspection it is running. A doctype that is not on this list is not refused
#: because attaching to it would be wrong in principle; it is refused because
#: nothing on this surface has a reason to, and an allowlist that grows only when
#: a screen needs it is the one that stays readable.
#:
#: `Employee` IS DELIBERATELY ABSENT even though the wizard files six documents
#: against one. `attach_onboarding_document` is that route: it takes a
#: `document_kind`, checks the HR role and files against the Employee the caller
#: named. Letting the general attach reach Employee would be a second door onto
#: personnel evidence with one fewer gate on it.
#:
#: EVERY ENTRY CARRIES A `company` COLUMN, and that is a requirement rather than
#: a coincidence: `guard.require_scoped_doc` reads exactly that column to decide
#: whether the caller may reach the record, so a doctype without one would be
#: scoped by nothing at all. `Asset State Log` is the register this rules out —
#: it is append-only by its own controller and has no company of its own, and a
#: photograph taken at a state change already has a home, in
#: `log_asset_state_change`'s own `photo_file_token`.
ATTACHABLE_DOCTYPES = (
	"Asset Register",
	"Farm Task",
	"Farm Task Assignment",
	"Expense Receipt",
	"Scale Ticket",
	"Inspection Session",
	"Housing Inspection",
	"Compliance Alert",
	"Spray REI",
	"Document Validation",
	# S10. THE PHOTOGRAPH OF THE SCENE, which this surface has been able to open
	# a report about since v0.88.0 and never able to put a picture on. The
	# omission read, from a handset, as "photo attachments are rejected on
	# accident reports" — and it was: `attach_file_to_document` refused the
	# doctype by name, not the file. A guard left down, a torn sleeve, the
	# ground where somebody fell: these are the evidence an OSHA 301 is
	# reconstructed from, they exist for about an hour before the scene is
	# cleared, and the only camera there is the one in somebody's pocket.
	#
	# IT MEETS THIS TUPLE'S OWN RULE — `Accident Report` carries a `company`
	# column, so `guard.require_scoped_doc` scopes it like every other entry
	# rather than passing every docname through.
	#
	# NO NEW PERMISSION IS MANUFACTURED. `create_accident_report` on this
	# surface is already open to any enrolled worker, deliberately and for the
	# same reason ("the person who finds somebody on the ground is whoever finds
	# them"), and `get_accident_report` is scoped and otherwise open too. A
	# worker who may open the report may photograph what it is about.
	ACCIDENT_REPORT,
)

#: Most bytes one attach carries in a request body. The chunked upload path
#: (`stage_file_chunk` → `finalize_staged_file`) is what a photograph should go
#: through — it verifies a SHA-256 before anything is written — and this ceiling
#: is here so the convenience door cannot quietly become the upload path for a
#: 40-megapixel plate photograph over a rural cell.
ATTACH_INLINE_LIMIT = 8 * 1024 * 1024


# ── 71. register_asset ───────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("register_asset", mutating=True, limit=guard.WRITE_LIMIT)
def register_asset(
	user: str,
	name=None,
	asset_type=None,
	company=None,
	location=None,
	parent_asset=None,
	description=None,
	nfc_uid=None,
	serial_number=None,
	model=None,
	acquired_on=None,
	purchase_value=None,
	replacement_value=None,
	gps_latitude=None,
	gps_longitude=None,
	irrigation_zone=None,
	service_interval_hours=None,
	service_interval_days=None,
	last_service_date=None,
	last_service_hours=None,
	photo_file_token=None,
) -> dict:
	"""Register a new asset from the field. The docname IS the printed tag ID.

	THE COMPANY IS THE CALLER'S, not the body's, wherever the body does not name
	one the caller can reach. `guard.require_company` is what makes that true —
	an account that can register a tractor into somebody else's entity is not
	scoped to anything, and an asset is the record every later inspection, spray
	log and insurance line hangs off.

	`photo_file_token` IS A `File` DOCNAME AND NOT BYTES, which is the same
	two-step every other upload on this surface takes: `stage_file_chunk` and
	`finalize_staged_file` move the bytes and verify a SHA-256, and this
	re-points what they produced at the new asset. A failed attach does NOT undo
	the registration — the reply carries `photo_error` and the asset name, and
	`attach_file_to_document` completes it.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed)

	tag = str(name or "").strip()
	if not tag:
		frappe.throw(
			"name is required — it is the tag ID that gets printed on the label and it is also "
			"the docname, so 'MC-Valve-05' on the sticker and 'MC-Valve-05' in the database are "
			"the same string.",
			frappe.ValidationError,
		)
	if not str(asset_type or "").strip():
		frappe.throw("asset_type is required.", frappe.ValidationError)

	inner = {"name": tag, "asset_type": str(asset_type).strip(), "company": entity}
	# BOTH SPELLINGS FORWARDED, NEITHER RESOLVED HERE. `asset_tags._parent`
	# refuses them only when they disagree, and a wrapper that picked a winner
	# would put a valve under the wrong turnout — which is what the closing
	# cascade then walks.
	for key, value in (
		("location", location),
		("parent_asset", parent_asset),
		("description", description),
		("nfc_uid", nfc_uid),
		("serial_number", serial_number),
		("model", model),
		("acquired_on", acquired_on),
		("irrigation_zone", irrigation_zone),
		("last_service_date", last_service_date),
		("photo_file_token", photo_file_token),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()
	for key, value in (
		("purchase_value", purchase_value),
		("replacement_value", replacement_value),
		("gps_latitude", gps_latitude),
		("gps_longitude", gps_longitude),
		("service_interval_hours", service_interval_hours),
		("service_interval_days", service_interval_days),
		("last_service_hours", last_service_hours),
	):
		if value is not None:
			inner[key] = value

	return asset_tags.register_asset(inner).data


# ── 72. generate_asset_qr ────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("generate_asset_qr", limit=guard.READ_LIMIT)
def generate_asset_qr(user: str, asset_name=None, format=None) -> dict:
	"""The QR for one asset's tag, as a base64 PNG or the raw matrix.

	THE BYTES TRAVEL IN THE ANSWER, exactly as the badge PNG, the `.pkpass` and
	the signed page do, and for the identical reason: this door authenticates
	with `X-FarmOps-Token` and a private `File` URL is a login page to it.

	`format` SHADOWS A BUILTIN AND KEEPS THE NAME ANYWAY. `routes.bind` reduces
	a body to the keys the signature declares, so spelling it `qr_format` here
	would drop the argument the tool documents and the app sends. The builtin is
	not used in this function.

	SCOPED ON THE WAY IN, not on the way out. `guard.require_scoped_doc` checks
	the asset exists and belongs to an entity this caller may reach before the
	tool reads it — an unscoped QR would let anybody holding a field credential
	mint a printable tag for another farm's machine.
	"""
	allowed = guard.require_scope(user)
	asset = guard.require_scoped_doc(asset_tags.ASSET_REGISTER, asset_name, "asset_name", allowed)

	inner = {"asset_name": asset}
	wanted = str(format or "").strip()
	if wanted:
		inner["format"] = wanted
	return asset_tags.generate_asset_qr(inner).data


# ── 73. attach_file_to_document ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("attach_file_to_document", mutating=True, limit=guard.WRITE_LIMIT)
def attach_file_to_document(
	user: str,
	doctype=None,
	name=None,
	file_name=None,
	file_content=None,
	file_url=None,
	is_private=None,
	dry_run=None,
) -> dict:
	"""File one attachment against a record a field app is allowed to write to.

	THE ALLOWLIST IS THE WHOLE REASON THIS WRAPPER EXISTS. The tool underneath
	attaches to ANY document on this site, and publishing that unmodified would
	put "grow the evidence on a submitted Journal Entry" and "add a page to a
	verified I-9" inside a field worker's credential. `ATTACHABLE_DOCTYPES` is
	the set of registers some other route on this surface already writes into,
	and everything else is refused by name with the list in the sentence.

	`allow_cancelled` IS NOT DECLARED AT ALL, so `routes.bind` cannot deliver it
	and the tool's own default (refuse) stands. A cancelled document is history;
	deciding to add to it anyway is a desk decision with the record open, not a
	checkbox on a handset.

	THE PARENT IS SCOPED BEFORE ANYTHING IS DECODED. `guard.require_scoped_doc`
	confirms the record exists and belongs to an entity this caller may reach —
	so an attachment cannot be filed against another farm's asset, and a caller
	cannot learn whether a docname exists there by watching which error comes
	back.

	`file_content` IS BASE64 AND IS CAPPED. The chunked path
	(`stage_file_chunk` → `finalize_staged_file`) is what a photograph should
	take, because it verifies a SHA-256 before anything is written; this door is
	for the small attachment that does not justify a session, and the cap stops
	it quietly becoming the upload path.
	"""
	allowed = guard.require_scope(user)

	target = str(doctype or "").strip()
	if not target:
		frappe.throw("doctype is required.", frappe.ValidationError)
	if target not in ATTACHABLE_DOCTYPES:
		frappe.throw(
			f"{target} is not something this app may attach to from a handset. The registers a "
			f"field device may file evidence against are: {', '.join(ATTACHABLE_DOCTYPES)}. "
			"Personnel documents go through attach_onboarding_document, which checks the HR role. "
			"Nothing was attached.",
			frappe.PermissionError,
		)

	docname = guard.require_scoped_doc(target, name, "name", allowed)
	label = str(file_name or "").strip()
	if not label:
		frappe.throw("file_name is required.", frappe.ValidationError)

	body = str(file_content or "").strip()
	url = str(file_url or "").strip()
	if body and len(body) > ATTACH_INLINE_LIMIT:
		frappe.throw(
			f"file_content is {len(body)} characters of base64, over the "
			f"{ATTACH_INLINE_LIMIT} this route carries inline. Upload it with stage_file_chunk "
			"and finalize_staged_file, which verify a SHA-256, then pass the file_url that "
			"returns. Nothing was attached.",
			frappe.ValidationError,
		)

	inner = {"doctype": target, "name": docname, "file_name": label}
	if body:
		inner["file_content"] = body
	if url:
		inner["file_url"] = url
	if is_private is not None:
		inner["is_private"] = is_private
	if dry_run is not None:
		inner["dry_run"] = dry_run

	return file_tools.attach_file_to_document(inner).data


# ══════════════════════════════════════════════════════════════════════════════
# Sprint 9 (v0.79.0) — interruption, investigation, discipline and wizards
#
# THE PAUSE PAIR IS THE ONE A FIELD WORKER USES MOST AND IS GATED LEAST. Pausing
# a job you are holding is your own work, so `guard.FARM_OPS_ROLES` is the whole
# gate — the same as `start_task` and `complete_task_via_mobile` above.
#
# THE INCIDENT METHODS SPLIT THREE WAYS SINCE v0.94.0, AND THEY USED TO BE ONE.
# All five carried `personnel.require_hr_role` on the reasoning that a discipline
# record is a personnel document and a picker has no business writing or reading
# one. Half of that is right and the other half was gating the wrong act. (The
# wrapper names below stay `*_discipline_*` — that is this file's own name for
# the endpoint, which is `farm_ops_method` and therefore the live URL the iOS
# app calls; the tools they call into are `create_incident_record` and its
# siblings in `tools/discipline.py`, renamed in v0.95.0.)
#
#   * REPORTING what happened — `create_discipline_record` — is `require_shift_role`.
#     Documenting an incident is the same shape as reporting an accident, and the
#     accident block eight paragraphs down already argues that at length.
#   * THE SUBJECT'S OWN RECORD — `acknowledge_discipline_record`,
#     `get_discipline_record`, `list_discipline_history` — is SELF-OR-HR, the
#     line `get_i9_form` draws, with the caller resolved through `Employee.user_id`
#     so the exception cannot be claimed by naming somebody.
#   * THE REGISTER ACROSS EVERYBODY — `get_discipline_report` — stays `HR_ROLES`.
#     It is not somebody reading their own file.
#
# The tools underneath still have no role check — on the MCP transport the
# operator's enablement switch stands in front of them, and a phone does not go
# through it.
#
# THE ACCIDENT METHODS SIT BETWEEN THE TWO, and the split is deliberate.
# `create_accident_report` is open to ANY enrolled worker: the person who finds
# somebody on the ground is whoever finds them, and a server that refused their
# report because they are not a foreman would be a server people work around at
# the exact moment that matters. Updating, closing and listing take the dispatch
# role — those are the investigation, and an investigation is somebody's job.
# ══════════════════════════════════════════════════════════════════════════════


# ── 74. pause_task_via_mobile ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("pause_task_via_mobile", mutating=True, limit=guard.WRITE_LIMIT)
def pause_task_via_mobile(user: str, task=None, task_assignment=None, reason=None) -> dict:
	"""Stop the clock on a job this worker is coming back to.

	SCOPED TO THE CALLER'S OWN WORK. `worker_id` is not on this signature, so
	`routes.bind` cannot deliver it and the tool's own holder check runs against
	the Employee this login resolves to — an account that could pause somebody
	else's job could stop a stranger's clock from across the farm.
	"""
	allowed = guard.require_scope(user)
	employee = _employee(user)

	inner = {"worker_id": employee}
	assignment = _assignment(task, task_assignment, allowed)
	if assignment:
		inner["assignment"] = assignment
	else:
		inner["task"] = guard.require_scoped_doc(FARM_TASK, task, "task", allowed)
	if reason:
		inner["reason"] = str(reason).strip()

	return dispatch.pause_farm_task(inner).data


# ── 75. resume_task_via_mobile ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("resume_task_via_mobile", mutating=True, limit=guard.WRITE_LIMIT)
def resume_task_via_mobile(user: str, task=None, task_assignment=None) -> dict:
	"""Pick a paused job back up. Whatever else was running is stood down."""
	allowed = guard.require_scope(user)
	employee = _employee(user)

	inner = {"worker_id": employee}
	assignment = _assignment(task, task_assignment, allowed)
	if assignment:
		inner["assignment"] = assignment
	else:
		inner["task"] = guard.require_scoped_doc(FARM_TASK, task, "task", allowed)

	return dispatch.resume_farm_task(inner).data


# ── 76. link_tasks_via_mobile ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("link_tasks_via_mobile", mutating=True, limit=guard.WRITE_LIMIT)
def link_tasks_via_mobile(user: str, task=None, linked_task=None, relationship=None, note=None) -> dict:
	"""Say that two jobs are the same thing, or are being worked together.

	OPEN TO ANY ENROLLED WORKER, unlike the merge below. Noticing that your job
	and somebody else's are the same valve is exactly the observation a worker in
	a block makes and a foreman at a desk does not — and a link changes no state,
	takes nothing off anybody's board and is undone by deleting a row.
	"""
	allowed = guard.require_scope(user)
	first = guard.require_scoped_doc(FARM_TASK, task, "task", allowed)
	second = guard.require_scoped_doc(FARM_TASK, linked_task, "linked_task", allowed)

	inner = {"task": first, "linked_task": second, "linked_by": _employee(user)}
	if relationship:
		inner["relationship"] = str(relationship).strip()
	if note:
		inner["note"] = str(note).strip()

	return dispatch.link_farm_tasks(inner).data


# ── 77. merge_task_via_mobile ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("merge_task_via_mobile", mutating=True, limit=guard.WRITE_LIMIT)
def merge_task_via_mobile(user: str, task=None, into=None, reason=None) -> dict:
	"""Fold a duplicate into the job that is actually being worked.

	FOREMAN AND ABOVE, and the asymmetry with `link_tasks_via_mobile` is the
	whole point: a link is an observation and a merge is a decision. It takes one
	worker's job off the board under somebody else's name, and `merge_farm_task`
	requires a reason for exactly that reason. THE SYSTEM SURFACES AND THE HUMAN
	DECIDES — `duplicate_hint` on a claim or a start is the surfacing; this is
	the decision, and nothing on this surface makes it automatically.
	"""
	guard.require_dispatch_role(user, "Merging a duplicate task")
	allowed = guard.require_scope(user)

	inner = {
		"task": guard.require_scoped_doc(FARM_TASK, task, "task", allowed),
		"into": guard.require_scoped_doc(FARM_TASK, into, "into", allowed),
		"reason": str(reason or "").strip(),
		"merged_by": _employee(user),
	}
	if not inner["reason"]:
		frappe.throw(
			"reason is required. A merge takes one worker's job off the board under another "
			"name, and the reason is what makes that reviewable six weeks later.",
			frappe.ValidationError,
		)

	return dispatch.merge_farm_task(inner).data


# ── 78. add_task_note_via_mobile ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("add_task_note_via_mobile", mutating=True, limit=guard.WRITE_LIMIT)
def add_task_note_via_mobile(
	user: str,
	doctype=None,
	name=None,
	task=None,
	narrative=None,
	note_type=None,
	source_language=None,
) -> dict:
	"""Append a written account to a task, an investigation or a discipline record."""
	allowed = guard.require_scope(user)
	employee = _employee(user)
	target, docname = _narrative_target(user, doctype, name, task, allowed)

	inner = {
		"doctype": target,
		"name": docname,
		"narrative": str(narrative or "").strip(),
		"author": employee,
		"author_name": _employee_identity(employee).get("employee_name") or employee,
	}
	if note_type:
		inner["note_type"] = str(note_type).strip()
	inner["source_language"] = str(source_language or "").strip() or _caller_language(user, employee)

	return narrative_tools.add_task_note(inner).data


# ── 78b. add_task_note ───────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("add_task_note", mutating=True, limit=guard.WRITE_LIMIT)
def add_task_note(
	user: str,
	doctype=None,
	name=None,
	task=None,
	note=None,
	narrative=None,
	language=None,
	source_language=None,
	audio_file_token=None,
	audio_file=None,
	audio_duration_seconds=None,
	note_type=None,
) -> dict:
	"""The same write as `add_task_note_via_mobile`, under the name the app calls.

	v0.98.0, ITEM 12, AND THE WHOLE BUG IS THE SUFFIX. `add_task_note_via_mobile`
	has been mounted since v0.79.0 and `Route` builds every path off the
	wrapper's own name, so the published path carried `_via_mobile` on it. The
	handset asks for `add_task_note` — the MCP tool's name, which is what every
	brief and every reader would predict — and got a 404, then tried
	`add_narrative`, `append_note`, `create_task_note`, `add_note`,
	`create_narrative_note` and `log_task_note` and got six more. `list_task_notes`
	IS published under its plain name, so a record's narrative could be read and
	not written: a foreman's field note stayed in `LocalNarrativeStore` on one
	phone, labelled "on this phone only", which is where every one of them still
	is.

	THE OLDER SPELLING KEEPS ITS ROUTE. A handset already in an orchard is not
	asked to change to get an answer — the same promise `submit_form_signature`
	kept for `collect_signature` and `list_org_reference_data` kept for
	`list_onboarding_reference_data`.

	IT DECLARES BOTH VOCABULARIES BECAUSE THIS TRANSPORT'S ARGUMENT FILTER IS
	UNFORGIVING. `routes.bind` keeps only the keys a signature names, so a method
	declaring `narrative` at a caller sending `note` is not a rename that half
	works — it is an empty note, written and stored, with nothing in the answer
	saying the words were dropped. `note`/`narrative` and `language`/
	`source_language` are the same argument twice, and either may be sent.

	`audio_file_token` IS THE HANDSET'S SPELLING OF A `File` DOCNAME, the one
	`finalize_staged_file` hands back — the same string `set_employee_photo` and
	`attach_onboarding_document` call `file_token`. SENDING ONE MAKES THIS A
	VOICE NOTE: the words go down as the transcription and the recording is
	linked, which is `attach_audio_note`'s job and is delegated to it rather than
	reimplemented, so a dictated note and a typed one land in one register with
	one set of rules. Without it this is the plain written entry.
	"""
	allowed = guard.require_scope(user)
	employee = _employee(user)
	target, docname = _narrative_target(user, doctype, name, task, allowed)

	words, _ = _one_spelling(note, narrative, "note", "narrative")
	tongue, _ = _one_spelling(language, source_language, "language", "source_language")
	recording, _ = _one_spelling(audio_file_token, audio_file, "audio_file_token", "audio_file")

	inner = {
		"doctype": target,
		"name": docname,
		"author": employee,
		"author_name": _employee_identity(employee).get("employee_name") or employee,
		"source_language": str(tongue or "").strip() or _caller_language(user, employee),
	}
	if note_type:
		inner["note_type"] = str(note_type).strip()

	if not recording:
		inner["narrative"] = str(words or "").strip()
		return narrative_tools.add_task_note(inner).data

	inner["transcription"] = str(words or "").strip()
	inner["audio_file"] = str(recording).strip()
	if audio_duration_seconds is not None:
		inner["audio_duration_seconds"] = audio_duration_seconds
	return narrative_tools.attach_audio_note(inner).data


# ── 79. attach_audio_note ────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("attach_audio_note", mutating=True, limit=guard.WRITE_LIMIT)
def attach_audio_note(
	user: str,
	doctype=None,
	name=None,
	task=None,
	transcription=None,
	audio_file=None,
	audio_duration_seconds=None,
	note_type=None,
	source_language=None,
) -> dict:
	"""File a voice note and the transcription the handset produced from it.

	THE CALL THIS SURFACE EXISTS FOR, on the day somebody is on the ground. The
	handset transcribes on-device — iOS's Speech framework runs locally, so a
	foreman in a block with no signal still has text — and this stores what it
	produced against the record.

	`audio_file` IS A `File` DOCNAME, not bytes. Minutes of audio over a rural
	cell is exactly what `stage_file_chunk` / `finalize_staged_file` exist for,
	and they verify a SHA-256 on the way. The transcription is written first, so
	a failed file link comes back as `audio_error` with the words already saved.

	THE LANGUAGE DEFAULTS TO THE SPEAKER'S OWN. On a bilingual crew, a Spanish
	account tagged as English is a translation nobody knows is needed — so where
	the client sends nothing, the Employee's `preferred_language` is used, and
	where that is empty the answer says the entry is untagged rather than
	guessing.
	"""
	allowed = guard.require_scope(user)
	employee = _employee(user)
	target, docname = _narrative_target(user, doctype, name, task, allowed)

	inner = {
		"doctype": target,
		"name": docname,
		"transcription": str(transcription or "").strip(),
		"author": employee,
		"author_name": _employee_identity(employee).get("employee_name") or employee,
	}
	if audio_file:
		inner["audio_file"] = str(audio_file).strip()
	if audio_duration_seconds is not None:
		inner["audio_duration_seconds"] = audio_duration_seconds
	if note_type:
		inner["note_type"] = str(note_type).strip()
	inner["source_language"] = str(source_language or "").strip() or _caller_language(user, employee)

	return narrative_tools.attach_audio_note(inner).data


# ── 80. list_task_notes ──────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_task_notes", limit=guard.READ_LIMIT)
def list_task_notes(user: str, doctype=None, name=None, task=None, limit=None) -> dict:
	"""A record's accumulated narrative, oldest first."""
	allowed = guard.require_scope(user)
	target, docname = _narrative_target(user, doctype, name, task, allowed)

	inner = {"doctype": target, "name": docname}
	if limit is not None:
		inner["limit"] = limit
	return narrative_tools.list_task_notes(inner).data


def _require_self_or_hr(user: str, record: str) -> None:
	"""The subject of this incident record may act on it; anybody else needs HR.

	THE SAME LINE `get_i9_form` DRAWS, and drawn the same way — the subject is
	read OFF THE RECORD and the caller off their own login, so there is nothing in
	a request that can assert the exception. A caller who is not the subject falls
	through to `require_hr_role` and gets its sentence.

	IT REFUSES BY FALLING THROUGH RATHER THAN BY DECIDING. A record whose
	`employee` cannot be read — a shape this site does not have, a row mid-write —
	leaves `subject` empty, which matches no caller and therefore requires HR.
	The failure direction is the safe one.
	"""
	try:
		subject = frappe.db.get_value(DISCIPLINE_RECORD, record, "employee")
	except Exception:  # pragma: no cover - a site shaping the column differently
		subject = None
	if not subject or str(subject) != str(fieldwork._employee_for(user) or ""):
		personnel.require_hr_role()


# ── 81. create_discipline_record ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_discipline_record", mutating=True, limit=guard.WRITE_LIMIT)
def create_discipline_record(
	user: str,
	employee=None,
	discipline_type=None,
	incident_date=None,
	incident_description=None,
	expected_improvement=None,
	followup_date=None,
	policy_violated=None,
	witnesses=None,
	consequence_of_no_improvement=None,
	supersedes_note=None,
	employee_statement=None,
	narrative=None,
	suspension_start=None,
	suspension_end=None,
	company=None,
	report_direction=None,
	direction=None,
	evidence_files=None,
) -> dict:
	"""Report one incident, from a handset — the farm's direction or the worker's.

	`direction` IS THE HANDSET'S SPELLING OF `report_direction`, v0.98.0, and it
	takes `disciplinary` / `grievance` as well as the column's own two words —
	see `REPORT_DIRECTIONS` for why one column and not two. `create_dispute`
	below is this method with the direction fixed, for the caller who does not
	want to know that a grievance and a warning share a register.

	SHIFT ROLE SINCE v0.94.0, AND THE RECLASSIFICATION IS THE POINT. This used to
	read "HR ROLE, NOT THE FIELD ROLE. A discipline record is a personnel
	document." Documenting what happened is REPORTING, not administration — and
	this codebase already contains that exact argument, forty lines further down
	this same file, about `create_accident_report`:

	    "the person who finds somebody on the ground is whoever finds them, and a
	    server that refused their report because they are not a foreman would be a
	    server people work around at the exact moment that matters."

	That shipped in v0.79.0. Discipline was gated the opposite way in the same
	sprint, in the same file. A foreman who watched the incident and cannot file
	it either does not file it or dictates it to somebody who did not see it, and
	both of those are worse records than the one this gate was protecting.

	READING somebody else's file is a different question and stays `HR_ROLES` —
	see `get_discipline_record` below, which draws the self-or-HR line
	`get_i9_form` draws. Initiating a report does not.

	`issued_by` IS NOT ON THIS SIGNATURE. It is the caller, resolved from the
	authenticated session — an account that could name somebody else as the
	issuing manager could put a supervisor's name on a warning they never gave,
	which is the one forgery this record is most exposed to. `reported_by` is
	resolved server-side by the tool for the same reason, and the two are distinct
	facts: widening this gate does not blur who reported what.

	`evidence_files` SINCE v0.96.0, IN THE SAME SHAPE `complete_task_via_mobile`
	TAKES. This route accepted no file token at all, so a foreman photographing
	the thing a warning is about had nowhere to send the photograph and the app
	kept it and said so. The tokens are the ones `finalize_staged_file` handed
	back; `_evidence` renames them and `discipline._file_the_evidence` hangs them
	off the record, privately, after it exists.

	IT IS NOT AN ALLOW-LIST ENTRY ON `attach_file_to_document`, which is the
	other way this could have gone and does not work: that route asks Frappe for
	`write` on the parent, and `Farm Incident Record` grants write to System
	Manager and HR Manager alone. A Foreman — the role this method was
	deliberately opened to in v0.94.0 — would have been refused at the second
	call having been allowed at the first. The evidence rides the create instead,
	on one gate.
	"""
	personnel.require_shift_role()
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed)
	issuer = _employee(user)

	inner = {
		"employee": _employee_argument(employee, allowed, "employee"),
		"discipline_type": str(discipline_type or "").strip(),
		"issued_by": issuer,
		"issued_by_name": _employee_identity(issuer).get("employee_name") or issuer,
	}
	if entity:
		inner["company"] = entity
	for key, value in (
		("incident_date", incident_date),
		("incident_description", incident_description),
		("expected_improvement", expected_improvement),
		("followup_date", followup_date),
		("policy_violated", policy_violated),
		("witnesses", witnesses),
		("consequence_of_no_improvement", consequence_of_no_improvement),
		("supersedes_note", supersedes_note),
		("employee_statement", employee_statement),
		("narrative", narrative),
		("suspension_start", suspension_start),
		("suspension_end", suspension_end),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()
	# NORMALISED BEFORE THEY ARE COMPARED, which `_one_spelling` alone could not
	# do: `report_direction="Worker Report"` and `direction="grievance"` are the
	# same instruction in two vocabularies, and a body carrying both would be
	# refused as a contradiction on the raw strings. Two directions that really
	# do differ are still refused, by the same helper, on the resolved values.
	settled = _one_spelling(
		_report_direction(report_direction),
		_report_direction(direction, "direction"),
		"report_direction",
		"direction",
	)[0]
	if settled:
		inner["report_direction"] = settled
	inner["source_language"] = _caller_language(user, issuer)
	evidence = _evidence(evidence_files)
	if evidence:
		inner["evidence_files"] = evidence

	return discipline_tools.create_incident_record(inner).data


# ── 81b. create_dispute ──────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_dispute", mutating=True, limit=guard.WRITE_LIMIT)
def create_dispute(
	user: str,
	employee=None,
	description=None,
	incident_description=None,
	incident_date=None,
	policy_violated=None,
	witnesses=None,
	employee_statement=None,
	narrative=None,
	company=None,
	evidence_files=None,
) -> dict:
	"""A worker raises something. Their words, their direction, no rung on a chain.

	v0.98.0, ITEM 12. THE REGISTER AND EVERY RULE ALREADY EXISTED AND THE NAME
	DID NOT, which is why this is thirty lines rather than a doctype.
	`Farm Incident Record` was built to carry both directions — v0.94.0 added
	`report_direction` and the four behaviours that hang off it — and no route
	and no argument on this surface could reach the worker's half. So the app
	filed a grievance through `create_discipline_record` at the lowest step with
	`DISPUTE RAISED BY …` typed into the description, and what that produces is a
	WARNING: a step on a progressive-discipline chain, in the file of the person
	who complained, escalating from nothing and escalated from by whatever comes
	next. `SERVER_CHANGES.md` says in as many words that it works and is not
	right.

	FOUR THINGS ARE DIFFERENT ABOUT A WORKER REPORT AND THE TOOL ENFORCES ALL
	FOUR. It carries no `discipline_type` — a warning level on somebody's own
	report files it as a step against them, and the tool refuses one by name. It
	gets no `prior_record` and no `step_number`, so it neither escalates from the
	last warning nor becomes the thing the next one escalates from. And it is NOT
	refused after a termination: a worker disputing the termination itself is
	precisely the report that must not come back "there is no step after the end
	of employment."

	`discipline_type` IS NOT ON THIS SIGNATURE AT ALL, so this transport's
	argument filter is what makes it unreachable rather than merely refused. The
	tool's refusal is the second lock and stays where it is.

	THE GATE IS THE SHIFT ROLE, the same one `create_discipline_record` has held
	since v0.94.0, and the argument is stronger here than there: the person who
	needs to raise a complaint is whoever has one, and a server that refused it
	because they hold a picker's credential is a server that receives no
	complaints and concludes there are none. READING the register is a different
	question and stays on `HR_ROLES` — `get_discipline_record` and
	`list_discipline_history` are unchanged.

	`employee` DEFAULTS TO THE CALLER, which is the ordinary case: somebody
	raising their own grievance on their own phone. Naming another employee is
	how a crew leader files on behalf of a picker who does not have the app or
	the words, and it is scoped by `_employee_argument` like every other employee
	argument here. `reported_by` is resolved server-side by the tool from the
	authenticated session either way, so who TYPED it is recorded whoever it is
	about.
	"""
	personnel.require_shift_role()
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed)
	author = _employee(user)

	subject = str(employee or "").strip()
	inner = {
		"employee": _employee_argument(subject, allowed, "employee") if subject else author,
		"report_direction": discipline_tools.WORKER_REPORT,
		"issued_by": author,
		"issued_by_name": _employee_identity(author).get("employee_name") or author,
	}
	if entity:
		inner["company"] = entity

	# `description` IS THE APP'S SPELLING and `incident_description` is the
	# tool's; the tool reads either, and both are declared here because `bind`
	# delivers only what a signature names.
	account, _ = _one_spelling(description, incident_description, "description", "incident_description")
	if account:
		inner["incident_description"] = account
	for key, value in (
		("incident_date", incident_date),
		("policy_violated", policy_violated),
		("witnesses", witnesses),
		("employee_statement", employee_statement),
		("narrative", narrative),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()
	inner["source_language"] = _caller_language(user, author)
	evidence = _evidence(evidence_files)
	if evidence:
		inner["evidence_files"] = evidence

	return discipline_tools.create_incident_record(inner).data


# ── 82. acknowledge_discipline_record ────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("acknowledge_discipline_record", mutating=True, limit=guard.WRITE_LIMIT)
def acknowledge_discipline_record(
	user: str,
	record=None,
	employee_signature=None,
	declined_to_sign=None,
	witnesses=None,
	employee_statement=None,
	manager_signature=None,
) -> dict:
	"""Record that the employee was told — or that they declined to sign.

	SELF-OR-HR SINCE v0.94.0. The subject signs their own acknowledgment, on their
	own phone: under the old gate an HR account had to be holding the pad for a
	worker to acknowledge a warning about themselves, which is the same conflation
	`collect_signature` was carrying and the same fix. The either/or rule the
	controller enforces — a signature or an explicit refusal, never silence
	dressed as agreement — is untouched and is what actually protects this record.

	THE SUBJECT IS RESOLVED FROM THE RECORD AND THE CALLER FROM THEIR LOGIN, so
	the exception cannot be claimed by naming somebody. Anybody who is not the
	subject still needs `HR_ROLES`.
	"""
	allowed = guard.require_scope(user)
	docname = guard.require_scoped_doc(DISCIPLINE_RECORD, record, "record", allowed)
	_require_self_or_hr(user, docname)

	inner = {"record": docname}
	if declined_to_sign is not None:
		inner["declined_to_sign"] = declined_to_sign
	for key, value in (
		("employee_signature", employee_signature),
		("witnesses", witnesses),
		("employee_statement", employee_statement),
		("manager_signature", manager_signature),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()

	return discipline_tools.acknowledge_incident_record(inner).data


# ── 83. get_discipline_record ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_discipline_record", limit=guard.READ_LIMIT)
def get_discipline_record(user: str, record=None) -> dict:
	"""One step in full, with its narrative and the step before it.

	SELF-OR-HR SINCE v0.94.0, following `get_i9_form` exactly. A worker may read a
	warning in their own file — this was the one personnel record with no
	`get_my_*` equivalent beside `get_my_w4`, `get_my_i9`, `list_my_pay_stubs` and
	`list_my_trainings`, and several states give an employee a statutory right to
	their own personnel file. Anybody else's record still takes `HR_ROLES`.
	"""
	allowed = guard.require_scope(user)
	docname = guard.require_scoped_doc(DISCIPLINE_RECORD, record, "record", allowed)
	_require_self_or_hr(user, docname)
	return discipline_tools.get_incident_record({"record": docname}).data


# ── 84. list_discipline_history ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_discipline_history", limit=guard.READ_LIMIT)
def list_discipline_history(user: str, employee=None, include_inactive=None, direction=None) -> dict:
	"""One employee's whole file, in order — both directions by default.

	SELF-OR-HR SINCE v0.94.0. A worker reading their own file gets BOTH
	directions, which is why the tool defaults that way: a grievance they raised
	in June is part of the story of a warning they got in July, and a view that
	showed one without the other is the version that suits whoever is holding the
	report. `direction` narrows it for a caller that wants one side.
	"""
	allowed = guard.require_scope(user)
	person = _employee_argument(employee, allowed, "employee")
	if person != fieldwork._employee_for(user):
		personnel.require_hr_role()
	inner: dict = {"employee": person}
	if include_inactive is not None:
		inner["include_inactive"] = include_inactive
	if direction not in (None, ""):
		inner["direction"] = str(direction).strip()
	return discipline_tools.list_incident_history(inner).data


# ── 85. get_discipline_report ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_discipline_report", limit=guard.READ_LIMIT)
def get_discipline_report(user: str, employee=None) -> dict:
	"""The chain as a document for legal or HR review, including its gaps.

	`HR_ROLES`, UNCHANGED BY v0.94.0 AND DELIBERATELY SO. The three reads beside
	it gained a self-service branch; this one did not, because it is not somebody
	reading their own record — it is the register across everybody, the document
	the module docstring calls "what an HR manager hands a lawyer". It is also
	SUPERVISOR DIRECTION ONLY: `chain_for` filters it, so a worker's own
	grievances can never appear in it as their disciplinary history.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	return discipline_tools.get_incident_report(
		{"employee": _employee_argument(employee, allowed, "employee")}
	).data


# ── 86. create_accident_report ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_accident_report", mutating=True, limit=guard.WRITE_LIMIT)
def create_accident_report(
	user: str,
	occurred_at=None,
	incident_description=None,
	severity=None,
	injured_person=None,
	injury_type=None,
	body_part=None,
	medical_treatment=None,
	witnesses=None,
	immediate_actions=None,
	location_doctype=None,
	location=None,
	location_description=None,
	asset=None,
	narrative=None,
	company=None,
) -> dict:
	"""Open an incident record at the scene.

	OPEN TO ANY ENROLLED WORKER, AND THAT IS THE ONE DESIGN DECISION ON THIS
	METHOD. The person who finds somebody on the ground is whoever finds them; a
	server that refused their report because they are not a foreman would be a
	server people work around at the exact moment that matters, and the account
	written at the scene is worth many times the one written in an office that
	evening. Everything AFTER the report — the investigation, the determination,
	the closure — takes the dispatch role.

	`reported_by` is the caller, not the body.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed)
	reporter = _employee(user)

	inner = {
		"occurred_at": str(occurred_at or "").strip(),
		"incident_description": str(incident_description or "").strip(),
		"reported_by": reporter,
		"reported_by_name": _employee_identity(reporter).get("employee_name") or reporter,
	}
	if entity:
		inner["company"] = entity
	if injured_person:
		inner["injured_person"] = _employee_argument(injured_person, allowed, "injured_person")
	if witnesses is not None:
		inner["witnesses"] = witnesses
	for key, value in (
		("severity", severity),
		("injury_type", injury_type),
		("body_part", body_part),
		("medical_treatment", medical_treatment),
		("immediate_actions", immediate_actions),
		("location_doctype", location_doctype),
		("location", location),
		("location_description", location_description),
		("asset", asset),
		("narrative", narrative),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()
	inner["source_language"] = _caller_language(user, reporter)

	return accident_tools.create_accident_report(inner).data


# ── 87. update_accident_investigation ────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("update_accident_investigation", mutating=True, limit=guard.WRITE_LIMIT)
def update_accident_investigation(
	user: str,
	report=None,
	narrative=None,
	note_type=None,
	root_cause=None,
	contributing_factors=None,
	corrective_actions=None,
	osha_recordable=None,
	osha_determination_basis=None,
	witnesses=None,
	statement_taken_from=None,
	followup_date=None,
	days_away_from_work=None,
	days_restricted_duty=None,
	severity=None,
	status=None,
) -> dict:
	"""Add what was learned. Called as many times as the investigation takes."""
	guard.require_dispatch_role(user, "Updating an accident investigation")
	allowed = guard.require_scope(user)
	author = _employee(user)

	inner = {
		"report": guard.require_scoped_doc(ACCIDENT_REPORT, report, "report", allowed),
		"author": author,
		"author_name": _employee_identity(author).get("employee_name") or author,
		"osha_determined_by": author,
	}
	if witnesses is not None:
		inner["witnesses"] = witnesses
	for key, value in (
		("narrative", narrative),
		("note_type", note_type),
		("root_cause", root_cause),
		("contributing_factors", contributing_factors),
		("corrective_actions", corrective_actions),
		("osha_recordable", osha_recordable),
		("osha_determination_basis", osha_determination_basis),
		("statement_taken_from", statement_taken_from),
		("followup_date", followup_date),
		("severity", severity),
		("status", status),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()
	for key, value in (
		("days_away_from_work", days_away_from_work),
		("days_restricted_duty", days_restricted_duty),
	):
		if value is not None:
			inner[key] = value
	inner["source_language"] = _caller_language(user, author)

	return accident_tools.update_accident_investigation(inner).data


# ── 88. close_accident_investigation ─────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("close_accident_investigation", mutating=True, limit=guard.WRITE_LIMIT)
def close_accident_investigation(
	user: str,
	report=None,
	corrective_actions=None,
	followup_date=None,
	osha_recordable=None,
	osha_determination_basis=None,
	closure_summary=None,
) -> dict:
	"""Close an investigation, with everything closing one requires."""
	guard.require_dispatch_role(user, "Closing an accident investigation")
	allowed = guard.require_scope(user)
	closer = _employee(user)

	inner = {
		"report": guard.require_scoped_doc(ACCIDENT_REPORT, report, "report", allowed),
		"closed_by": closer,
		"closed_by_name": _employee_identity(closer).get("employee_name") or closer,
	}
	for key, value in (
		("corrective_actions", corrective_actions),
		("followup_date", followup_date),
		("osha_recordable", osha_recordable),
		("osha_determination_basis", osha_determination_basis),
		("closure_summary", closure_summary),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()

	return accident_tools.close_accident_investigation(inner).data


# ── 89. get_accident_report ──────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_accident_report", limit=guard.READ_LIMIT)
def get_accident_report(user: str, report=None) -> dict:
	"""One investigation in full: witnesses, narrative, steps, what is outstanding."""
	allowed = guard.require_scope(user)
	return accident_tools.get_accident_report(
		{"report": guard.require_scoped_doc(ACCIDENT_REPORT, report, "report", allowed)}
	).data


# ── 90. list_accident_reports ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_accident_reports", limit=guard.READ_LIMIT)
def list_accident_reports(
	user: str,
	status=None,
	open_only=None,
	severity=None,
	from_date=None,
	to_date=None,
	company=None,
	limit=None,
) -> dict:
	"""The incident register, filterable."""
	guard.require_dispatch_role(user, "Reading the accident register")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed)

	inner = {}
	if entity:
		inner["company"] = entity
	for key, value in (
		("status", status),
		("severity", severity),
		("from_date", from_date),
		("to_date", to_date),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()
	if open_only is not None:
		inner["open_only"] = open_only
	if limit is not None:
		inner["limit"] = limit

	data = accident_tools.list_accident_reports(inner).data
	data["reports"] = guard.scoped(data.get("reports") or [], allowed)
	data["report_count"] = len(data["reports"])
	return data


#: The fourteen types a Wizard Definition may carry, mapped onto the seven the
#: handset can draw. v0.91.0.
#:
#: A TYPE ABSENT FROM THIS TABLE IS PASSED THROUGH UNCHANGED, ON PURPOSE. The
#: app decodes an unknown type as `unsupported` and draws a row saying the field
#: needs a newer build — and, if the field is required, refuses to submit rather
#: than posting a record with a hole in it. That is the right answer for the
#: three types no iOS control collects: `employee_select` and `asset_select`
#: need a roster or an asset register the seven cannot express, and
#: `multi_select` would keep one of the several answers somebody gave. Calling
#: any of them `select` would draw an empty picker with no way forward, which
#: reads as a broken app rather than a missing one.
#:
#: THE SAME GOES FOR A TYPE NOBODY HAS ADDED YET. A `geo` on a Wizard Field this
#: build has never heard of is passed through for the same reason rather than
#: defaulted to `text`: a text box would ask a worker to TYPE a location and
#: file the sentence they typed where coordinates belong, and nothing anywhere
#: would report it. A field with NO type is the opposite case and `describe()`
#: has already made it `text` before this table is consulted — a blank on a
#: record somebody was filling in is a text box, and guessing there is right.
#:
#: `datetime` LOSES THE TIME OF DAY, which is the one lossy entry here. The
#: alternative is refusing the accident wizard's first required field and taking
#: the whole flow down with it; a date is worth more than nothing, and the app
#: posts date-only strings a Frappe Datetime accepts.
_IOS_FIELD_TYPES = {
	"text": "text",
	"long_text": "text",
	"number": "number",
	"date": "date",
	"datetime": "date",
	"select": "select",
	"checkbox": "select",
	"photo": "photo",
	"signature": "signature",
	"qr_scan": "qr",
	# The recording is the alternative to typing, never the only way to answer —
	# every seeded `audio_note` sits beside a `long_text` that asks the same
	# question. A text box collects the same sentence from a build with no
	# recorder in it.
	"audio_note": "text",
}

#: What a `checkbox` becomes once it is a two-choice picker. The values are what
#: gets posted, and they are `1`/`0` rather than `Yes`/`No` because the endpoint
#: on the other end reads the answer with `cint`.
_IOS_CHECKBOX_OPTIONS = (
	{"value": "1", "label_en": "Yes", "label_es": "Sí"},
	{"value": "0", "label_en": "No", "label_es": "No"},
)


def _ios_bilingual(row: dict, key: str, en: str, es: str) -> None:
	"""Write the English and the Spanish into their own slots.

	THIS IS THE SECOND PASS THE FIRST VERSION SAID IT WANTED. Until v0.92.2 both
	slots got the SAME resolved string, because the wrapper only ever had one:
	`get_wizard_definition` picks the worker's language off their Employee record
	and answers in it. That worked on a handset set to the language the server
	guessed and lied on every other one — a picker whose phone said Spanish got
	the English sentence out of `label_es` with nothing marking it as English,
	which is worse than a blank, and `fully_translated` could report a gap the
	payload had already papered over.

	The strings now come from two real passes — `describe(doc, "en")` and
	`describe(doc, "es")` — so a `tr:` key resolves through `Farm Translation`
	once per language rather than being copied across.

	A SPANISH THAT IS ONLY ENGLISH IS SENT AS ABSENT. `describe` falls back to
	English when there is no translation, so an unconditional `_es` would tell
	the app a Spanish string exists for every field on the site. `null` is what
	`WizardLabel.pick` already handles — it falls back to English itself — and
	it is the difference between "nobody wrote this yet" and "somebody did".
	"""
	english = str(en or "").strip()
	spanish = str(es or "").strip()
	row[key + "_en"] = english
	row[key + "_es"] = spanish if spanish and spanish != english else None


def _wizard_pass(inner: dict, language: str) -> dict | None:
	"""The same spec described again in one named language.

	THE SECOND AND THIRD READS OF A DOCUMENT THE CALLER IS ALREADY CLEARED FOR.
	The gates ran on the first call in `get_wizard_definition`; this asks the
	same tool for the same wizard with `language` pinned, so there is no
	widening here — a caller who could not read the spec never reaches this.

	NEVER RAISES. A missing translation, a `Farm Translation` table mid-migrate
	or anything else that makes one language unreadable must not take down a
	form that renders perfectly well in the other. `None` means "no strings from
	this pass", and the callers fall back to what they already had.
	"""
	try:
		return wizard_tools.get_wizard_definition({**inner, "language": language}).data
	except Exception:  # pragma: no cover - a translation gap is not a broken form
		return None


def _ios_by_key(rows, key: str) -> dict:
	"""The Spanish pass indexed by the key its English twin is matched on.

	MATCHED BY KEY RATHER THAN BY POSITION. The two passes are the same document
	described twice, so the orders agree today — but a spec whose steps a
	translator reordered, or a field a condition dropped from one pass and not
	the other, would silently pair a Spanish label with a different English
	question. That is the one failure mode of a zip that nobody notices, because
	every string is present and only the pairing is wrong.
	"""
	out = {}
	for row in rows or []:
		if isinstance(row, dict):
			out[str(row.get(key) or "")] = row
	return out


def _ios_wizard_field(field: dict, spanish: dict | None = None) -> dict:
	"""One control, in the shape `WizardField` decodes.

	THE KEY IS THE TARGET FIELD, NOT THE FIELDNAME, and they are the same thing
	until an operator says otherwise. `key` is what the app posts the answer
	under, and Wizard Field carries `target_field` for exactly the case where
	the question's name on the form and the column it lands in differ — a wizard
	that set one and got its answers posted under the other would file every
	record with the field it cares about empty.
	"""
	row = dict(field)
	# iOS HAS NO CONDITIONAL LOGIC AND IS NOT BEING SENT ANY. `visible_if` is a
	# rule the app cannot evaluate; leaving it on the wire invites a later build
	# to half-implement it against a spec nobody validated.
	row.pop("visible_if", None)

	server_type = str(row.get("type") or "").strip().lower()
	row["server_field_type"] = server_type
	row["type"] = _IOS_FIELD_TYPES.get(server_type, server_type)

	es = spanish or {}
	row["key"] = row.get("target_field") or row.get("fieldname") or ""
	_ios_bilingual(row, "label", row.get("label"), es.get("label"))
	# THE HELP TEXT BECOMES THE PLACEHOLDER WHEN THERE IS NO PLACEHOLDER. The app
	# has nowhere else to put a field's help, and "In your own words. Be specific"
	# is the difference between a usable answer and three words.
	_ios_bilingual(
		row,
		"placeholder",
		row.get("placeholder") or row.get("help") or "",
		es.get("placeholder") or es.get("help") or "",
	)
	row["required"] = bool(row.get("required"))

	es_options = _ios_by_key(es.get("options"), "value")
	options = []
	for option in row.get("options") or []:
		label = str(option.get("label") or option.get("value") or "")
		value = option.get("value") or ""
		entry = {"value": value}
		es_option = es_options.get(str(value)) or {}
		_ios_bilingual(entry, "label", label, es_option.get("label"))
		options.append(entry)
	if server_type == "checkbox" and not options:
		options = [dict(option) for option in _IOS_CHECKBOX_OPTIONS]
	row["options"] = options
	return row


def _ios_wizard_step(step: dict, spanish: dict | None = None) -> dict:
	"""One page of the form, in the shape `WizardStep` decodes."""
	row = dict(step)
	es = spanish or {}
	row.pop("visible_if", None)
	row["key"] = row.get("step_key") or ""
	_ios_bilingual(row, "title", row.get("title"), es.get("title"))
	# The server calls it a description and the app calls it help. Same sentence,
	# same place on the screen — under the step title.
	_ios_bilingual(row, "help", row.get("description") or "", es.get("description") or "")

	es_fields = _ios_by_key(es.get("fields"), "fieldname")
	row["fields"] = [
		_ios_wizard_field(dict(field), es_fields.get(str(field.get("fieldname") or "")))
		for field in (row.get("fields") or [])
	]
	return row


def _ios_wizard_spec(data: dict, spanish: dict | None = None, english: dict | None = None) -> dict:
	"""The server's spec, in the shape the handset decodes. v0.91.0.

	NOTHING RENDERED BEFORE THIS. `describe()` answers `wizard_key`, one resolved
	`title`, `step_key`, `fieldname` and fourteen field types; `WizardDefinition`
	decodes `name`, `title_en`/`title_es`, `key`, `key` again and seven. Every
	key the app looks for was absent, so a server-authored spec decoded to an
	empty name with no steps and `isRenderable` was false for all five of them —
	the same shape a record nobody filled in would have, which is why the failure
	looked like an empty register rather than a translation that was never
	written.

	THE TRANSLATION IS ADDITIVE AND THE SERVER'S OWN KEYS STAY. `wizard_key`,
	`step_key`, `fieldname`, `validation`, `default` and the rest travel
	untouched beside the iOS ones: the app ignores keys it does not declare,
	`_with_submit_endpoint` reads `wizard_key` and `submit_method` off this dict
	after the fact, and anybody debugging a spec against the MCP tool wants the
	two shapes to be comparable. `visible_if` is the one thing removed, because
	it is a rule rather than a datum.

	THE MCP TOOL IS NOT TOUCHED. `tools/wizards.py` answers the same fourteen
	types it always did to an MCP client, which has no handset and no seven-type
	renderer. This is the sidecar's translation, in the sidecar, beside the
	`submit_method` translation that had the same argument behind it.
	"""
	spanish = spanish or {}
	# THE ENGLISH COMES FROM ITS OWN PASS, NOT FROM `data`. `data` is resolved in
	# the CALLER'S language, so for a Spanish-reading picker its `title` is
	# Spanish — writing that into `title_en` would label Spanish as English. The
	# scalars stay `data`'s (`title`, `language`, `untranslated` describe the
	# language this worker was determined to read); only the strings the app
	# renders are taken from the explicit passes.
	base = english or data
	data["name"] = data.get("wizard_key") or ""
	_ios_bilingual(data, "title", base.get("title"), spanish.get("title"))

	es_steps = _ios_by_key(spanish.get("steps"), "step_key")
	data["steps"] = [
		_ios_wizard_step(dict(step), es_steps.get(str(step.get("step_key") or "")))
		for step in (base.get("steps") or [])
	]
	return data


#: The one route a wizard posts to, and the method name behind it. v0.91.0.
SUBMIT_WIZARD = "submit_wizard_via_mobile"


def _wizard_answers(raw) -> dict:
	"""The answers dict, however the transport delivered it.

	A JSON BODY ARRIVES AS A DICT AND A FORM-ENCODED ONE AS A STRING, and both
	reach this transport — `fallback_auth` exists because the app sends its
	credential three ways for the same reason. Parsing a string here rather than
	refusing it keeps the endpoint working from `curl` and from the Desk console,
	which is where somebody debugging a wizard actually is.

	ANYTHING THAT IS NOT AN OBJECT IS REFUSED RATHER THAN COERCED. A list of
	answers has no keys to unpack and would silently file an empty record —
	which is the exact failure this whole endpoint exists to end.

	`user` AND `_auth` ARE DROPPED HERE TOO. Neither can survive
	`accepted_arguments` (which excludes `user`) or `guard.endpoint` (which pops
	both), so this is a third lock on the one pair that would matter — and it
	keeps them out of the `ignored` list, where they would read as an authoring
	mistake rather than as envelope somebody sent by habit.
	"""
	if raw in (None, ""):
		return {}
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except (json.JSONDecodeError, ValueError, TypeError):
			raise ToolError(
				"answers is not valid JSON. Send an object keyed by the wizard's field keys, like "
				'{"occurred_at": "2026-08-17", "severity": "First Aid"}. Nothing was written.'
			) from None
	if not isinstance(raw, dict):
		raise ToolError(
			"answers must be an object keyed by the wizard's field keys — the `key` each field "
			"carries in its spec. Nothing was written."
		)
	return {
		str(name): value for name, value in raw.items() if str(name) not in ("user", fallback_auth.BODY_KEY)
	}


def wizard_submit_route(method: str):
	"""The Route behind a wizard's `submit_method`, or None if it has none.

	The route table is the authority on what a handset may reach, and reading it
	here is what keeps this wrapper from widening that surface by one path: a
	`submit_method` naming a tool nobody routed resolves to nothing, exactly as
	it did when the app posted to the target directly.
	"""
	from ..farmops_api import routes as route_table

	if not method:
		return None
	for route in route_table.ROUTES:
		if route.path.rsplit("/", 1)[-1] == method:
			return route
	return None


def _wizard_answer_keys(data: dict) -> list:
	"""Every key the app will post an answer under, in the order they are asked."""
	return [
		str(field.get("key") or field.get("target_field") or field.get("fieldname") or "")
		for step in data.get("steps") or []
		for field in step.get("fields") or []
	]


def _with_submit_endpoint(data: dict) -> dict:
	"""Where the answers go, and which of them the target will actually take.

	THE TOOL DOES NOT KNOW WHAT A URL IS AND IS NOT BEING TAUGHT. `submit_method`
	is a tool name — `create_accident_report` — and it means the same thing to an
	MCP client, which has no sidecar and no prefix. The translation belongs where
	the prefix is known, which is here, so the MCP tool's shape is unchanged.

	THE PATH IS THE WRAPPER'S AND NOT THE TARGET'S, WHICH IS THE v0.91.0 CHANGE.
	The app posts `{"wizard": …, "answers": {…}}`, and `create_accident_report`
	declares neither of those names, so `routes.bind` — which keeps the body keys
	that match the handler's signature and drops the rest — delivered an empty
	call. Every answer a worker gave went in the bin at the door and the endpoint
	answered as though it had been asked for nothing. `submit_wizard_via_mobile`
	is the one method that speaks that envelope; it unpacks the answers and calls
	the target through the SAME route table and the SAME argument filter, so
	nothing is reachable through it that was not reachable before.

	A METHOD WITH NO ROUTE PRODUCES NO ENDPOINT, DELIBERATELY. The app treats a
	spec with an empty `submit_endpoint` as unrenderable and refuses to draw it,
	which is the failure worth having: a worker who is never shown the form has
	lost nothing, and a worker who fills in three steps and a signature before the
	post 404s has lost the thing this whole surface exists to collect. The reason
	travels in `submit_unavailable` so the screen can say which flow is down
	rather than showing an empty state that reads as "nothing to do".

	`submit_unmapped` NAMES THE ANSWERS THE TARGET CANNOT TAKE, and it is here
	rather than only in the submit response because that is the moment somebody
	can still do something about it. Three of the five shipped wizards ask for
	something their endpoint has no parameter for — `progressive_discipline` asks
	for two signatures, `inspection_session` for findings and photographs,
	`employee_onboarding` for the language the worker reads — and those answers
	are dropped by the argument filter today exactly as they were dropped before.
	Filing the rest is strictly better than filing nothing, which is what
	happened until now, so this REPORTS rather than refuses; the fix is a
	parameter on the target or a field the wizard stops asking for, and both are
	somebody's decision rather than this function's.

	`submit_context` is EMPTY and is sent anyway. Wizard Definition has no field
	for it — the doctype carries `submit_method` and nothing else about
	submission — so there is nothing to fill it with, and sending the key means
	the app's decoder gets the shape it declares rather than falling back to a
	default that would look identical to a context somebody forgot to configure.
	"""
	from ..farmops_api import routes as route_table

	method = str(data.get("submit_method") or "").strip()
	data["submit_context"] = {}
	data["submit_unmapped"] = []
	if not method:
		data["submit_endpoint"] = ""
		data["submit_unavailable"] = (
			f"wizard {data.get('wizard_key')!r} names no submit_method, so there is nowhere to "
			f"file it. An operator sets one on the Wizard Definition."
		)
		return data

	route = wizard_submit_route(method)
	if route is None:
		data["submit_endpoint"] = ""
		data["submit_unavailable"] = (
			f"wizard {data.get('wizard_key')!r} submits to {method!r}, which this app does not "
			f"publish to handsets. The flow cannot be filed from a phone until it is routed."
		)
		return data

	accepted = route_table.accepted_arguments(route.handler)
	data["submit_endpoint"] = f"farmops/api/mobile/{SUBMIT_WIZARD}"
	data["submit_unmapped"] = sorted({key for key in _wizard_answer_keys(data) if key not in accepted})
	return data


# ── 91. get_wizard_definition ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_wizard_definition", limit=guard.READ_LIMIT)
def get_wizard_definition(user: str, wizard=None, language=None) -> dict:
	"""One wizard's spec, in this worker's own language.

	THE LANGUAGE IS THE CALLER'S AND IS NOT TAKEN FROM THE BODY BY DEFAULT. The
	Employee record says which language this person reads — it is asked at hire
	and it is a compliance field, because hazard communication has to be in a
	language the worker understands. A `language` argument is still accepted, for
	the case of a supervisor reading a flow in English to check it before their
	crew sees it in Spanish.
	"""
	guard.require_scope(user)
	employee = _employee(user)
	inner = {"wizard": str(wizard or "").strip(), "employee": employee, "user": user}
	if language:
		inner["language"] = str(language).strip()

	data = wizard_tools.get_wizard_definition(inner).data
	# THE HANDSET GETS BOTH LANGUAGES AND PICKS, WHICH IS NOT WHAT THE TOOL DOES.
	# v0.92.2: the app decodes `title_en`/`title_es` and switches on a setting
	# the server cannot see, so one resolved string put the WRONG WORDS in the
	# other slot rather than leaving it empty — a Spanish-reading picker read
	# English out of `label_es` with nothing marking it as English.
	#
	# ENGLISH IS ASKED FOR EXPLICITLY RATHER THAN TAKEN FROM `data`. For that
	# same picker `data` holds Spanish, and copying it into `title_en` would
	# label it English — the same lie in the other direction.
	#
	# The caller's own resolution still travels untouched: `language`, `title`
	# and `untranslated` remain the tool's answer for the language this worker
	# was determined to read.
	english = _wizard_pass(inner, "en")
	spanish = _wizard_pass(inner, "es")
	# RESHAPED FIRST, ENDPOINT SECOND. `_with_submit_endpoint` reads `wizard_key`
	# and `submit_method` off the dict, and `_ios_wizard_spec` leaves both where
	# they were — but the order is the one that survives a later reshape that
	# does not.
	return _with_submit_endpoint(_ios_wizard_spec(data, spanish, english))


# ── 92. list_wizard_definitions ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_wizard_definitions", limit=guard.READ_LIMIT)
def list_wizard_definitions(user: str, category=None, language=None) -> dict:
	"""What flows this handset can render, titled in the worker's language."""
	guard.require_scope(user)
	employee = _employee(user)
	inner = {"employee": employee, "user": user}
	if category:
		inner["category"] = str(category).strip()
	if language:
		inner["language"] = str(language).strip()
	return wizard_tools.list_wizard_definitions(inner).data


# ── 92b. submit_wizard_via_mobile ────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint(SUBMIT_WIZARD, mutating=True, limit=guard.WRITE_LIMIT)
def submit_wizard_via_mobile(user: str, wizard=None, wizard_key=None, answers=None) -> dict:
	"""File a finished wizard: unpack its answers and call the target it names.

	THE PAYLOAD HAD NOWHERE TO LAND. The app posts one envelope for every
	wizard — `{"wizard": "accident_investigation", "answers": {…}}` — because it
	does not and must not know what an accident report's parameters are called.
	`routes.bind` keeps the body keys that MATCH THE HANDLER'S SIGNATURE and
	drops the rest, and `create_accident_report` declares neither `wizard` nor
	`answers`, so the whole envelope was dropped at the door and the target was
	called with nothing at all. Not a 404 and not a refusal: a successful call
	that filed an empty record. This method is the one that speaks the envelope.

	IT IS NOT THE DISPATCHER `routes.py` REFUSES, AND THE DIFFERENCE IS THE POINT.
	The objection to a `submit_wizard` was that it puts the permission decision in
	the wrong place — a Housing Inspection and a Farm Incident Record have different
	guards, and one route in front of both would decide for both. Nothing here
	decides anything:

	  * THE TARGET IS NOT NAMED BY THE CALLER. It is read off the Wizard
	    Definition's `submit_method`, which only an operator with Desk access
	    sets. A body naming its own destination would be the dispatcher.
	  * THE TARGET MUST BE ON THE ROUTE TABLE. `wizard_submit_route` walks the
	    same closed list `app.py` resolves paths against, so the reachable set is
	    exactly the methods a phone could already post to directly — this adds
	    no path to that surface and cannot.
	  * THE TARGET'S OWN GUARD STILL RUNS. `route.handler` is the
	    `@guard.endpoint`-wrapped function, gates and all: its role check, its
	    scope check, its rate limit, its audit row, and the authenticated caller
	    injected as `user` by its own decorator rather than by this one.
	  * THE TARGET'S OWN ARGUMENT FILTER STILL RUNS. The answers are reduced by
	    `routes.accepted_arguments(route.handler)` — the identical filter
	    `routes.bind` would have applied — so `worker`, `foreman`, `record_data`
	    and a W-4's `status` stay exactly as unreachable from a phone as they
	    were.

	What is left over is reported rather than filed. `ignored` names every answer
	the target has no parameter for, and there are real ones: `progressive_discipline`
	collects two signatures `create_discipline_record` cannot take, and
	`inspection_session` collects findings and photographs `start_inspection`
	cannot. Those answers were being dropped before this method existed too — the
	difference is that the response now says which, and `get_wizard_definition`
	says so before a worker fills anything in. FILING THE REST IS RIGHT: the
	alternative is refusing three of the five shipped flows outright, and a
	discipline record with no signature attached is worth more than no discipline
	record and a worker who typed it twice.
	"""
	from ..farmops_api import routes as route_table

	guard.require_scope(user)
	key = str(wizard or wizard_key or "").strip()
	if not key:
		raise ToolError(
			"submit_wizard_via_mobile needs a wizard — send `wizard` (or `wizard_key`) naming the "
			"Wizard Definition these answers were collected against."
		)

	# THE SPEC IS THE AUTHORITY ON WHERE THIS GOES, and reading it through the
	# tool is what makes an unknown or withdrawn wizard refuse here in exactly
	# the sentence the read refuses in. A worker whose form was withdrawn between
	# opening it and finishing it should be told that and not have it filed.
	spec = wizard_tools.get_wizard_definition({"wizard": key, "employee": _employee(user), "user": user}).data
	method = str(spec.get("submit_method") or "").strip()
	route = wizard_submit_route(method)
	if route is None:
		raise ToolError(
			f"wizard {key!r} submits to {method or '<nothing>'!r}, which this app does not publish "
			f"to handsets, so there is nowhere to file it. Nothing was written. An operator sets "
			f"`submit_method` on the Wizard Definition to a method that is routed."
		)

	given = _wizard_answers(answers)
	accepted = route_table.accepted_arguments(route.handler)
	unpacked = {name: value for name, value in given.items() if name in accepted}
	ignored = sorted(set(given) - set(unpacked))

	result = route.handler(**unpacked)
	return {
		"wizard": key,
		"submit_method": method,
		"filed": True,
		# NAMED, NOT COUNTED. "3 answers were ignored" sends whoever reads it
		# back to the wizard to work out which three; the names are what an
		# operator needs to add the parameter or drop the question.
		"ignored": ignored,
		"accepted_count": len(unpacked),
		"result": result if isinstance(result, dict) else {"value": result},
	}


# ── 93. list_shipments ───────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_shipments", limit=guard.READ_LIMIT)
def list_shipments(user: str, company=None, status=None, open_only=None, limit=None) -> dict:
	"""What is going out, for the entities this caller may reach. v0.80.0.

	Read-only and scoped like everything else here. A driver wants the loads that
	have not arrived yet, which is what `open_only` answers.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed)

	inner = {}
	if entity:
		inner["company"] = entity
	if status not in (None, ""):
		inner["status"] = str(status).strip()
	if open_only is not None:
		inner["open_only"] = open_only
	if limit is not None:
		inner["limit"] = limit

	data = shipment_tools.list_shipments(inner).data
	data["shipments"] = guard.scoped(data.get("shipments") or [], allowed)
	data["count"] = len(data["shipments"])
	return data


# ── 94. get_shipment ─────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_shipment", limit=guard.READ_LIMIT)
def get_shipment(user: str, shipment=None) -> dict:
	"""One shipment and its paperwork, in this caller's own language. v0.80.0."""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc("Trade Shipment", shipment, "shipment", allowed)
	employee = _employee(user)
	return shipment_tools.get_shipment({"shipment": name, "employee": employee, "user": user}).data


# ── 95. get_shipment_readiness ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_shipment_readiness", limit=guard.READ_LIMIT)
def get_shipment_readiness(user: str, shipment=None) -> dict:
	"""What is still missing before this load can go. v0.80.0.

	The question somebody standing next to a truck actually has, and the reason
	this is on the handset at all.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc("Trade Shipment", shipment, "shipment", allowed)
	return shipment_tools.get_shipment_readiness({"shipment": name}).data


# ── 96. list_trade_documents ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_trade_documents", limit=guard.READ_LIMIT)
def list_trade_documents(user: str, shipment=None, status=None, outstanding_only=None, limit=None) -> dict:
	"""The paperwork on a load, or the paperwork outstanding across them. v0.80.0."""
	allowed = guard.require_scope(user)

	inner = {}
	if shipment not in (None, ""):
		inner["shipment"] = guard.require_scoped_doc("Trade Shipment", shipment, "shipment", allowed)
	if status not in (None, ""):
		inner["status"] = str(status).strip()
	if outstanding_only is not None:
		inner["outstanding_only"] = outstanding_only
	if limit is not None:
		inner["limit"] = limit

	data = shipment_tools.list_trade_documents(inner).data
	data["documents"] = guard.scoped(data.get("documents") or [], allowed)
	data["count"] = len(data["documents"])
	return data


# ── 97. confirm_shipment_movement ────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("confirm_shipment_movement", limit=guard.WRITE_LIMIT, mutating=True)
def confirm_shipment_movement(user: str, shipment=None, movement=None, occurred_at=None, notes=None) -> dict:
	"""A handset confirms a load LEFT or ARRIVED. Nothing else. v0.80.0.

	NAMED FOR WHAT A PHONE DOES rather than after the tool it delegates to, and
	the two are deliberately not the same shape. `update_shipment_status` can
	release a shipment to Ready to Ship — which is the module's one gate — and can
	cancel one, and can carry an `override_reason` that walks past an incomplete
	document checklist. NONE OF THOSE IS FORWARDED.

	The reason is the same one that keeps `cancel=true` off `reject_farm_task`: a
	release is an assertion that the paperwork is in order, made by somebody with
	a trade role at a desk, and an account that could make it from a phone in a
	yard would make the gate worth nothing. A driver saying "I have left" and "I
	have arrived" is a different act, it is one nobody needs a certificate to
	perform, and it is the only one published here.

	`departed` and `delivered` are the two words the app sends; they map to the
	statuses the tool knows. A shipment that has not been released yet cannot be
	departed, and the tool's own transition table is what refuses it — this
	wrapper adds no rules of its own beyond the two it will not forward.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc("Trade Shipment", shipment, "shipment", allowed)

	moves = {
		"departed": "In Transit",
		"in transit": "In Transit",
		"delivered": "Delivered",
		"arrived": "Delivered",
	}
	wanted = moves.get(str(movement or "").strip().casefold())
	if not wanted:
		raise ToolError(
			"movement is 'departed' or 'delivered'. A handset confirms that a load left and "
			"that it arrived; releasing a shipment and cancelling one are desk acts with a "
			"trade role behind them and are not reachable from here. Nothing was changed."
		)

	inner = {"shipment": name, "status": wanted}
	if occurred_at not in (None, ""):
		inner["departed_on" if wanted == "In Transit" else "delivered_on"] = str(occurred_at).strip()
	if notes not in (None, ""):
		inner["notes"] = str(notes).strip()

	data = shipment_tools.update_shipment_status(inner).data
	return {
		"shipment": data.get("shipment"),
		"status": data.get("status"),
		"previous_status": data.get("previous_status"),
		"movement": movement,
		"changed": data.get("changed"),
	}


# ── 98. log_shift_event ──────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("log_shift_event", mutating=True, limit=guard.WRITE_LIMIT)
def log_shift_event(
	user: str,
	shift=None,
	event_type=None,
	event_datetime=None,
	description=None,
	logged_by=None,
	weather_snapshot_temp_f=None,
	weather_snapshot_heat_index_f=None,
	evidence_file_token=None,
) -> dict:
	"""One thing the supervisor did about the conditions, logged where it happened.

	THE TIMELINE IS THE EVIDENCE AND THE PHONE IS WHERE IT IS WRITTEN. `-1131`
	does not ask whether water was available in principle; it asks what happened
	during the shift, and four timestamped breaks answer that in a way an annual
	policy never can. The tool has existed since v0.19.3 and had no route, so the
	only way to log one was the Desk — which is to say, in the evening, from
	memory, which is exactly the record an investigator discounts.

	`log_shift_break` IS NOT THIS AND BOTH ARE PUBLISHED. A break carries a
	payroll classification and a duration and is the thing the handset's break
	coach counts from; this is everything else on the timeline — a supervisor
	observation, a heat-illness signs check, a shade trailer that broke down.

	`producer_record_doctype` AND `producer_record_name` ARE NOT FORWARDED. They
	point one compliance record at another and are how a packet builder follows a
	trail; a body that could set them could file this event as the product of a
	record it had nothing to do with. The MCP surface keeps them.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_SHIFT, shift, "shift", allowed)

	inner = {"shift": name, "event_type": event_type}
	for key, value in (
		("event_datetime", event_datetime),
		("description", description),
		("weather_snapshot_temp_f", weather_snapshot_temp_f),
		("weather_snapshot_heat_index_f", weather_snapshot_heat_index_f),
		("evidence_file_token", evidence_file_token),
	):
		if value is not None:
			inner[key] = value
	if logged_by is not None:
		# The lead worker who actually called the break, when it was not the
		# foreman — scoped like every other employee argument on this surface, so
		# an account cannot credit somebody in another entity with the call.
		inner["logged_by"] = _employee_argument(logged_by, allowed, "logged_by")

	result = shifts.log_shift_event(inner)
	return result.data


# ── 99. log_shift_location ───────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("log_shift_location", mutating=True, limit=guard.UPLOAD_LIMIT)
def log_shift_location(
	user: str,
	shift=None,
	latitude=None,
	longitude=None,
	lat=None,
	lon=None,
	timestamp=None,
	accuracy_meters=None,
	source=None,
	employee=None,
	notes=None,
) -> dict:
	"""Append one GPS fix to a shift's track. Appends only; never edits.

	THE ONE WRITE ON THIS SURFACE THE WORKER'S PHONE DRIVES RATHER THAN THE
	FOREMAN'S, and it is not a hole in the sole-actor rule. That rule is about who
	is ANSWERABLE — who forms the crew, calls the water break, signs the close —
	and none of it moves. A breadcrumb attests to nothing; it records where a
	device was, which is a measurement, and the foreman's record is the thing it
	corroborates.

	ONE FIX PER CALL, IN THE SHAPE A PHONE ACTUALLY SENDS, and on `UPLOAD_LIMIT`
	rather than `WRITE_LIMIT` for the reason `sync_bucket_entries` is: a handset
	that walked a canyon posts its backlog one call at a time the moment the bars
	return, and ten a minute would refuse most of it — throwing away the half of
	the track that is hardest to collect. `lat`/`lon` are accepted beside the full
	spellings because that is what a phone's location API calls them.

	IT IS LIVE ON THIS SURFACE AND DOES NOT READ `allow_log_shift_location`, which
	is the same posture every other method on this transport takes. The per-tool
	switches answer "what may the AI do"; a phone is not the AI, and the gates
	that hold here are the four in `guard` — the mobile kill switch, a named
	human, a Farm Ops role, and an Active grant. An operator who wants no crew
	tracking on a handset revokes the grant or leaves the app's tracking off; the
	MCP surface still honours the switch, so an AI client cannot post a fix on a
	site that has not opened it.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_SHIFT, shift, "shift", allowed)

	inner = {"shift": name}
	for key, value in (
		("latitude", latitude),
		("longitude", longitude),
		("lat", lat),
		("lon", lon),
		("timestamp", timestamp),
		("accuracy_meters", accuracy_meters),
		("source", source),
		("notes", notes),
	):
		if value is not None:
			inner[key] = value
	if employee is not None:
		inner["employee"] = _employee_argument(employee, allowed)

	result = shifts.log_shift_location(inner)
	return result.data


# ── 100. get_shift_track ─────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_shift_track", limit=guard.READ_LIMIT)
def get_shift_track(user: str, shift=None, employee=None, limit=None) -> dict:
	"""Where the crew went during one shift, in the order the fixes were taken.

	NOT THE ORDER THEY ARRIVED. A phone out of signal posts an hour of
	breadcrumbs at once, and a track sorted by insertion draws the crew standing
	still all morning where the signal came back and then teleporting across the
	farm.

	THE GAPS ARE REPORTED, because a track's silences are the part a reader
	misjudges. `limit` is passed through to the tool's own `TRACK_CAP`, which is
	deliberately much larger than the register cap — a nine-hour shift at a fix
	every thirty seconds is eleven hundred points, and cutting at five hundred
	would lose the afternoon without saying which half went.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_SHIFT, shift, "shift", allowed)

	inner = {"shift": name}
	if employee is not None:
		inner["employee"] = _employee_argument(employee, allowed)
	if limit is not None:
		inner["limit"] = limit

	result = shifts.get_shift_track(inner)
	return result.data


# ── 101. get_shift_crew_timeline ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_shift_crew_timeline", limit=guard.READ_LIMIT)
def get_shift_crew_timeline(user: str, shift=None, employee=None) -> dict:
	"""Each crew member's own envelope: their span, their weather, their events.

	THE SHIFT IS ONE RECORD AND THE CREW IS NOT ONE PERSON. `get_shift` answers
	what happened on this shift; this answers what happened TO ANA, who joined at
	09:40, left at 13:00, and was therefore present for two of the shift's five
	water breaks and absent for the hour it was hottest. Every number is computed
	against the worker's OWN span — the foreman's 96 °F at three in the afternoon
	is not evidence about a picker who went home at one.

	IT IS THE READ BEHIND THE CLOSE SCREEN. A supervisor about to sign a shift off
	is attesting to the crew's day and not to their own, and until this route
	existed the phone had no way to show them the difference.

	`employee` NARROWS IT TO ONE ENVELOPE and nothing else; it cannot widen the
	read past the shift the caller already named and was scoped against.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(FARM_SHIFT, shift, "shift", allowed)

	inner = {"shift": name}
	if employee is not None:
		inner["employee"] = _employee_argument(employee, allowed)

	result = shifts.get_shift_crew_timeline(inner)
	return result.data


# ── 102. scan_valve ──────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("scan_valve", mutating=True, limit=guard.WRITE_LIMIT)
def scan_valve(
	user: str,
	qr_data=None,
	content=None,
	scan=None,
	raw=None,
	code=None,
	toggle=None,
	expect_state=None,
	notes=None,
	company=None,
	gps_lat=None,
	gps_lon=None,
) -> dict:
	"""Scan a valve tag and, if asked in the same call, open or shut it.

	SCAN-TO-ACTION IN ONE POST, WITH THE ACTION OPT-IN. A worker walks up to a
	gate, points the camera at it and wants two things: to know what it is doing,
	and to change it. Those are two calls on this transport today —
	`universal_scan` then `log_asset_state_change` — and the second needs an
	action name the first does not hand over, because `log_asset_state_change`
	wants `close_valve` and the phone has to work out that the valve was open to
	know that. So this route resolves the tag, reads the state, and picks the
	action itself.

	`toggle` DEFAULTS TO FALSE AND THAT IS THE WHOLE SAFETY OF IT. A scan is
	looking at a thing; opening water onto a block is a decision, and closing a
	main is a decision that dries out every valve beneath it. A camera that
	fires on recognition would otherwise water a block because somebody walked
	past it with a phone. The answer's `next_action` is the button, and posting
	again with `toggle: true` is what pressing it does.

	`expect_state` IS FOR THE SCREEN THAT WAS DRAWN A MINUTE AGO. Pass the state
	the phone last showed and a valve somebody else has since moved is refused
	rather than toggled the wrong way round — on a valve that is the difference
	between watering a block and drying it out. It is optional because the
	scan-and-toggle-in-one-gesture flow has no stale reading to guard against:
	the state it acts on is the one it just read.

	THE COMPANY IS THE CALLER'S. Taken from the scope check rather than the body,
	so a valve tag belonging to another entity resolves as though it were not
	there — the same answer `universal_scan` gives a tag from another site, and
	the reason a scan cannot be used to enumerate the register next door.

	WHAT IS WRITTEN WITHOUT `toggle`: the scan stamp only — `last_scan_at`,
	`last_scan_by` and, where the handset sent a fix, the valve's GPS position.
	The route is declared mutating because of exactly that stamp, and metered at
	`WRITE_LIMIT` rather than `universal_scan`'s read limit because the branch
	that matters here is the one that opens water.
	"""
	allowed = guard.require_scope(user)
	scanned = str(qr_data or content or scan or raw or code or "").strip()
	if not scanned:
		frappe.throw(
			"qr_data is required — the string the scanner read. It may be the tag's full URL "
			"or the bare valve ID from the manual-entry box.",
			frappe.ValidationError,
		)

	scoped = _company(user, company, allowed)
	inner = {"qr_data": scanned, "scanned_by": user, "company": scoped}
	if gps_lat is not None:
		inner["gps_lat"] = gps_lat
	if gps_lon is not None:
		inner["gps_lon"] = gps_lon

	data = dict(valve_tools.scan_valve_qr(inner).data)

	# A REFUSED TOGGLE TAKES THE SCAN STAMP DOWN WITH IT, and that is the
	# framework's transaction rather than a choice made here: a `ValidationError`
	# out of the toggle rolls the request back, `last_scan_at` included. What
	# survives is the AUDIT row — `guard.endpoint` commits its failure rows on
	# their own transaction precisely so a refusal cannot erase the evidence of
	# itself — so "somebody stood at this gate and was refused" is recorded in
	# MCP Action Log even though the valve's own scan column reads as though the
	# call never happened. Do not read `last_scan_at` as a record of attempts; it
	# is a record of completed scans.
	data["toggled"] = False
	if not frappe.utils.cint(toggle):
		return data

	change = {"name": data["name"], "performed_by": user, "company": scoped}
	if expect_state is not None:
		change["expect_state"] = str(expect_state)
	if notes is not None:
		change["notes"] = str(notes)
	if gps_lat is not None:
		change["gps_lat"] = gps_lat
	if gps_lon is not None:
		change["gps_lon"] = gps_lon

	toggled = dict(valve_tools.toggle_irrigation_valve(change).data)
	toggled["scanned"] = data["scanned"]
	toggled["resolved_from"] = data["resolved_from"]
	toggled["entity_type"] = data["entity_type"]
	toggled["scan_recorded"] = True
	toggled["toggled"] = True
	return toggled


# ── 103. get_translation_bundle ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_translation_bundle", limit=guard.READ_LIMIT)
def get_translation_bundle(user: str, language=None, category=None, key_prefix=None) -> dict:
	"""Every string this handset needs, in the worker's language, in one call.

	WHAT THE HANDSET PULLS AT LOGIN instead of asking for one string at a time.
	Forty phones each fetching a label when they need it is forty phones with a
	loading spinner where a button should be; this is one call whose answer is
	cached on the device until the next login.

	THE LANGUAGE IS THE WORKER'S, AND `Accept-Language` IS THE FALLBACK. In that
	order, and never the other way round: this app's claim to have communicated a
	hazard "in a manner the employee can understand" (OSHA 1910.1200(h), WPS 40
	CFR 170.501) rests on `Employee.preferred_language` — a column somebody
	filled in about a person — and not on a device setting. A phone set to
	English by whoever handed it over says nothing about who is holding it now.

	The header IS honoured where the column is empty, because a site that has not
	filled it in yet is better served by the phone's guess than by English, and
	because a worker who set their own phone to Spanish has told us something.
	`language_source` says which of the two answered, so "why is this person
	seeing English" is answerable without reading the server.

	A MISSING TRANSLATION IS SERVED AS ENGLISH AND IS LISTED IN `untranslated`.
	Never a blank, never the raw key, never a refusal. A blank is a screen a
	worker cannot act on; a raw key is what a system shows when it has given up;
	a refusal locks a crew out of a flow over one sentence. `untranslated` is
	what makes the gap findable from the Desk rather than discoverable by
	somebody standing in front of a screen they cannot read.

	`category` narrows to one group — "Task Types", "Shift Status", "Error
	Messages" — and `key_prefix` narrows to a dotted prefix, which is the same
	thing for a client that thinks in keys. Both optional; the whole catalogue is
	small enough to send.
	"""
	from ..tools import translations

	employee = fieldwork._employee_for(user) or ""
	resolved, source = _response_language(language, employee)

	group = str(category or "").strip()
	prefix = str(key_prefix or "").strip()

	# `bundle` MERGES the English fall-back in, which is exactly what the handset
	# wants and exactly why the result cannot answer "what fell back". So the
	# register is asked separately for the keys that genuinely exist in this
	# language, and the gap is the difference.
	strings = translations.bundle(resolved, category=group, prefix=prefix)
	untranslated: list = []
	if resolved != translations.DEFAULT_LANGUAGE:
		have = set(_native_keys(resolved, group, prefix))
		untranslated = sorted(key for key in strings if key not in have)

	out = {
		"language": resolved,
		"language_source": source,
		"employee": employee or None,
		"category": group or None,
		"key_prefix": prefix or None,
		"count": len(strings),
		"strings": strings,
		"untranslated": untranslated,
		"untranslated_count": len(untranslated),
		"languages_available": list(translations.LANGUAGES),
		"default_language": translations.DEFAULT_LANGUAGE,
	}
	if untranslated:
		out["translation_note"] = (
			f"{len(untranslated)} string(s) have no {resolved} translation on this site and are "
			f"served in {translations.DEFAULT_LANGUAGE}. They are listed so the gap is findable "
			"from the Desk — a screen half in one language is worse than one consistently in the "
			"wrong one, and a blank is worse than both."
		)
	if source == "header":
		out["language_note"] = (
			"This worker's Employee record has no preferred_language, so the phone's "
			"Accept-Language header was used. THAT IS A GUESS AND NOT EVIDENCE: hazard "
			"communication and pesticide safety training have to be delivered in a language the "
			"worker understands, and proving that means the column, not the device. Ask them and "
			"fill it in."
		)
	elif source == "default" and not employee:
		out["language_note"] = (
			"This login has no Employee record and the phone sent no Accept-Language, so this is "
			"English by default rather than by anybody's choice."
		)
	return out


def _native_keys(language: str, category: str = "", prefix: str = "") -> list:
	"""Which keys genuinely exist in `language` on this site. Never raises.

	Separate from `translations.bundle` because that function's whole job is to
	MERGE the fall-back in, which makes its result unable to answer "what fell
	back". One query, and the answer is what `untranslated` is computed from.
	"""
	from ..tools import translations

	filters = {"language": language, "enabled": 1}
	if category:
		filters["category"] = category
	if prefix:
		filters["translation_key"] = ["like", f"{prefix}%"]
	try:
		return [
			str(key)
			for key in frappe.db.get_all(translations.DOCTYPE, filters=filters, pluck="translation_key") or []
		]
	except Exception:  # pragma: no cover - a site mid-migrate
		return []


# ══════════════════════════════════════════════════════════════════════════════
# v0.91.0 — the shadow log on a handset
#
# THE FEED IS ADDRESSED, AND THE ADDRESS IS THE WHOLE GATE. Every row in
# `Shadow Log Entry` names a `recipient_employee`: it is one person's copy of
# something that happened below them, not a register anybody may read. So all
# three wrappers resolve the recipient from the AUTHENTICATED SESSION and none
# of them declares `employee` — the tool's own filter takes a recipient docname
# and would happily hand over a colleague's feed, which is a supervisor's view
# of their own crew and nobody else's business.
#
# THE DOCNAME IS GUESSABLE, WHICH IS WHY THE DETAIL PAIR RE-CHECKS. `shadow_key`
# is built from the event, the source and the recipient —
# `Shift Closed::Farm Shift::SHIFT-2026-00042::HR-EMP-0003` — so a caller who
# knows a colleague's Employee ID can compose a docname rather than discover it.
# `guard.require_scoped_doc` proves the row is inside the caller's entities and
# stops there; `_shadow_entry` is what proves it is addressed to THEM.
#
# A ROW THAT IS NOT YOURS READS AS NOT FOUND, in the same words as one that does
# not exist. That is this file's standing rule and it matters more here than
# usual: a refusal worded "that is somebody else's copy" would confirm the row
# exists, which is exactly what a composed docname is fishing for.
#
# `acknowledge_shadow_log` IS THE MUTATING ONE. It is declared
# `mutating=True` at `guard.WRITE_LIMIT`, which is what puts it in the route
# table as a write and meters it at ten a minute rather than a read's rate.
# THE ACKNOWLEDGEMENT SAYS "I SAW THIS", and only the person it was addressed to
# can truthfully say it — an account that could acknowledge somebody else's copy
# could clear a supervisor's unread feed from across the farm and leave the
# record asserting they had read it.
# ══════════════════════════════════════════════════════════════════════════════


def _shadow_entry(name, user: str, allowed: list) -> str:
	"""One Shadow Log Entry docname, proved to exist, to be in scope, AND to be the caller's.

	Three checks, and the third is the one this helper exists for.
	`guard.require_scoped_doc` answers "is this a real row in an entity this
	account may reach" — which for a doctype whose docname is COMPOSED from the
	recipient's own Employee ID is not enough on its own. A picker who knows
	their foreman's employee number can write down a `shadow_key` without ever
	having been shown one.

	THE REFUSAL IS `DoesNotExistError` AND IS WORDED AS A MISS, matching
	`guard.require_scoped_doc` exactly. Saying "that copy is not addressed to
	you" would confirm the row is there, and a composed docname that draws a
	different error from a nonexistent one has learned something.
	"""
	docname = guard.require_scoped_doc(SHADOW_LOG_ENTRY, name, "name", allowed)
	recipient = str(frappe.db.get_value(SHADOW_LOG_ENTRY, docname, "recipient_employee") or "")
	if recipient != _employee(user):
		frappe.throw(f"name {docname} was not found.", frappe.DoesNotExistError)
	return docname


# ── 104. list_shadow_log_entries ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_shadow_log_entries", limit=guard.READ_LIMIT)
def list_shadow_log_entries(
	user: str,
	level=None,
	acknowledged=None,
	event_type=None,
	source_doctype=None,
	source_name=None,
	subject_employee=None,
	company=None,
	limit=None,
) -> dict:
	"""What happened below this supervisor, frozen as it was at the time.

	THE CALLER'S OWN FEED AND NOBODY ELSE'S. The tool's `employee` argument names
	the RECIPIENT, and it is deliberately not on this signature — so
	`routes.bind` cannot deliver it and the recipient is always the Employee this
	login resolves to. An account that could name somebody else would be reading
	a colleague's whole view of their crew: who was disciplined, whose shift ran
	long, whose bucket count was corrected.

	`acknowledged=false` IS THE CALL THE SCREEN ACTUALLY MAKES — the unread
	badge. The unfiltered call is the history behind it.

	AN EMPTY FEED IS A REAL ANSWER and the tool says so in `empty_note`: nobody
	may report to this person, the feed may be switched off in ERPNext MCP
	Settings, or nothing may have happened below them. A handset that drew "no
	entries" as an error would send foremen looking for a fault that is not
	there.

	`subject_employee` NARROWS WITHIN THE CALLER'S OWN FEED and cannot widen it.
	It is checked as a scoped Employee docname for the ordinary reason — a
	misspelling should refuse rather than quietly return nothing, which on a feed
	whose empty answer is meaningful would be indistinguishable from good news.
	"""
	allowed = guard.require_scope(user)

	inner: dict = {"employee": _employee(user)}
	entity = guard.require_company(user, company, allowed)
	if entity:
		inner["company"] = entity
	if level is not None:
		inner["level"] = level
	if acknowledged is not None:
		inner["acknowledged"] = acknowledged
	for key, value in (
		("event_type", event_type),
		("source_doctype", source_doctype),
		("source_name", source_name),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()
	if subject_employee not in (None, ""):
		inner["subject_employee"] = _employee_argument(subject_employee, allowed, "subject_employee")
	if limit is not None:
		inner["limit"] = limit

	data = dict(shadow_log_tools.list_shadow_log_entries(inner).data)
	# BELT TO THE TOOL'S BRACES, the same one every list on this surface gets.
	# The filter above already restricts to this recipient inside this caller's
	# entities; this drops anything that reached the result another way.
	data["entries"] = guard.scoped(data.get("entries") or [], allowed)
	return data


# ── 105. get_shadow_log_entry ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_shadow_log_entry", limit=guard.READ_LIMIT)
def get_shadow_log_entry(user: str, name=None) -> dict:
	"""One copy in full: the frozen snapshot, and what the source says now.

	ADDRESSED TO THE CALLER OR NOT FOUND — see `_shadow_entry`. The docname is
	composed from the recipient's own Employee ID, so scope alone would let a
	worker who knows a colleague's number read their copies by writing the key
	out rather than by being shown it.

	THE TWO HALVES OF THE ANSWER ARE REPORTED SEPARATELY AND NEVER MERGED. The
	snapshot is what this person was shown; `source_still_exists` and the tool's
	own `integrity_warning` are what is true now. A reader who cannot see the two
	disagree has the feed a notification would have given them, which is the
	thing this doctype exists not to be.
	"""
	allowed = guard.require_scope(user)
	return shadow_log_tools.get_shadow_log_entry({"name": _shadow_entry(name, user, allowed)}).data


# ── 106. acknowledge_shadow_log ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("acknowledge_shadow_log", mutating=True, limit=guard.WRITE_LIMIT)
def acknowledge_shadow_log(user: str, name=None, note=None, acknowledged_at=None) -> dict:
	"""Record that this recipient has seen their copy. One-way, and safe to retry.

	ONLY THE PERSON IT WAS ADDRESSED TO. `_shadow_entry` is the whole of the
	gate and it is a stronger claim than the read pair need: "I saw this" is a
	statement somebody makes about themselves, and an account that could make it
	on another person's behalf could empty a supervisor's unread feed from
	across the farm while leaving the record asserting they had read every row.
	There is no operator switch behind that on this transport — the MCP surface
	has one and a phone does not go through it — so the check is here.

	RETRY-SAFE BY THE TOOL'S OWN DESIGN, not by anything added here. A second
	call on an already-acknowledged row changes nothing, returns the existing
	acknowledgement and says `x_idempotent: true`. That matters on a handset more
	than anywhere: a phone that lost its response in a dead spot must not have to
	choose between retrying and being correct.

	`acknowledged_at` IS FOR THE QUEUED HANDSET CATCHING UP, so a batch posted
	when signal came back is stamped when it was read rather than when it
	uploaded. `note` is optional on purpose — an acknowledgement is not a review,
	and requiring a sentence makes it something nobody does.
	"""
	allowed = guard.require_scope(user)

	inner = {"name": _shadow_entry(name, user, allowed)}
	for key, value in (("note", note), ("acknowledged_at", acknowledged_at)):
		if value not in (None, ""):
			inner[key] = str(value).strip()

	return shadow_log_tools.acknowledge_shadow_log(inner).data


# ── stock & inventory ────────────────────────────────────────────────────────
#
# THE FOUR READS AND THE ONE WRITE THE INVENTORY TAB HAS BEEN 404ING ON SINCE
# v0.69.0. The tools have existed that whole time and none of them had a route,
# so every screen under `FarmOps/Features/Inventory` put the sidecar's own
# "is not a Farm Ops API method" 404 into an error banner and called it a day.
#
# THE SCOPING IS NOT UNIFORM ACROSS THE FIVE AND CANNOT BE, because the tools
# hand back three different row shapes:
#
#   * `get_stock_balance` rows carry `company`, so `guard.scoped` is the whole
#     of it.
#   * `get_warehouse_summary` describes ONE warehouse and its rows carry no
#     company at all — the entity is the warehouse's own, checked once.
#   * `get_stock_ledger` and `list_reorder_alerts` rows carry `warehouse` and no
#     company, and `get_stock_ledger` TAKES NO COMPANY ARGUMENT AT ALL — there is
#     no filter to ask the tool for. So the wrapper resolves this caller's
#     entities to a warehouse set and filters on that. Without it an account
#     scoped to one company reads every movement on the site, which is precisely
#     what `guard.scoped` prevents on the shapes that do carry a company.
#
# EVERY TOTAL IS RECOMPUTED FROM WHAT SURVIVED THE FILTER. The tools sum before
# this wrapper drops anything, so passing their `total_qty`, `total_value` or
# `net_qty_change` through unchanged would report another entity's quantities as
# a number after its rows had gone — the leak outliving the rows it came from.
def _allowed_warehouses(allowed: list) -> set:
	"""Every Warehouse docname in the entities this caller may reach."""
	names: set = set()
	for company in allowed or []:
		names.update(stock_tools._company_warehouses(company))
	return names


def _warehouse_scoped(rows: list, permitted: set) -> list:
	"""Drop rows held in a warehouse this caller may not reach.

	A row with NO warehouse is KEPT, for the same reason `guard.scoped` keeps a
	row with no company: a pre-v12 reorder rule is stored flat on the Item with
	no warehouse at all, and it is a site-wide rule rather than another entity's
	secret. Hiding it would make it invisible instead of fixed.
	"""
	out = []
	for row in rows or []:
		if not isinstance(row, dict):
			continue
		warehouse = str(row.get("warehouse") or "").strip()
		if warehouse and warehouse not in permitted:
			continue
		out.append(row)
	return out


# ── 107. get_stock_balance ───────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_stock_balance", limit=guard.READ_LIMIT)
def get_stock_balance(user: str, item_code=None, warehouse=None, company=None) -> dict:
	"""One item's position across the warehouses this caller may reach.

	The question somebody standing in a chemical shed has: is there enough of
	this to finish the block. Read-only.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed)

	inner: dict = {"item_code": str(item_code or "").strip()}
	if entity:
		inner["company"] = entity
	if warehouse not in (None, ""):
		inner["warehouse"] = str(warehouse).strip()

	data = stock_tools.get_stock_balance(inner).data
	data["balances"] = guard.scoped(data.get("balances") or [], allowed)
	data["warehouse_count"] = len(data["balances"])
	data["total_qty"] = round(sum(row["qty"] for row in data["balances"]), 6)
	data["total_value"] = round(sum(row["stock_value"] for row in data["balances"]), 2)
	return data


# ── 108. get_warehouse_summary ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_warehouse_summary", limit=guard.READ_LIMIT)
def get_warehouse_summary(user: str, warehouse=None, company=None) -> dict:
	"""Everything on hand in one warehouse, with its reorder rules.

	THE ENTITY CHECK IS ON THE WAREHOUSE'S OWN COMPANY AND HAPPENS AFTER THE
	READ, because the warehouse is the argument and its company is not knowable
	without resolving it. `guard.scoped` is no use here: the rows are items in a
	single shed and carry no company of their own, so one refusal for the whole
	answer is the shape this one takes.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed)

	inner: dict = {"warehouse": str(warehouse or "").strip()}
	if entity:
		inner["company"] = entity

	data = stock_tools.get_warehouse_summary(inner).data
	owner = str(data.get("company") or "").strip()
	if owner and owner not in allowed:
		raise frappe.PermissionError(
			f"warehouse {data.get('warehouse')!r} belongs to {owner}, which is not one of this "
			f"account's entities. Nothing was read."
		)
	return data


# ── 109. get_stock_ledger ────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_stock_ledger", limit=guard.READ_LIMIT)
def get_stock_ledger(
	user: str,
	item_code=None,
	warehouse=None,
	from_date=None,
	to_date=None,
	limit=None,
) -> dict:
	"""Movements, newest first. The audit trail, not a balance.

	THE TOOL HAS NO COMPANY ARGUMENT, so the entity filter is this wrapper's
	entirely — see the section note above. `truncated` is left as the tool set
	it: it describes whether the QUERY hit its limit, which is still true of the
	rows read even after some were dropped here.
	"""
	allowed = guard.require_scope(user)

	inner: dict = {}
	for key, value in (
		("item_code", item_code),
		("warehouse", warehouse),
		("from_date", from_date),
		("to_date", to_date),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()
	if limit is not None:
		inner["limit"] = limit

	data = stock_tools.get_stock_ledger(inner).data
	data["movements"] = _warehouse_scoped(data.get("movements") or [], _allowed_warehouses(allowed))
	data["count"] = len(data["movements"])
	data["net_qty_change"] = round(sum(row["qty_change"] for row in data["movements"]), 6)
	return data


# ── 110. list_reorder_alerts ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_reorder_alerts", limit=guard.READ_LIMIT)
def list_reorder_alerts(user: str, company=None, warehouse=None) -> dict:
	"""What has fallen below its reorder line, worst shortfall first."""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed)

	inner: dict = {}
	if entity:
		inner["company"] = entity
	if warehouse not in (None, ""):
		inner["warehouse"] = str(warehouse).strip()

	data = stock_tools.list_reorder_alerts(inner).data
	data["alerts"] = _warehouse_scoped(data.get("alerts") or [], _allowed_warehouses(allowed))
	data["count"] = len(data["alerts"])
	return data


# ── 111. create_stock_entry ──────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_stock_entry", mutating=True, limit=guard.WRITE_LIMIT)
def create_stock_entry(
	user: str,
	entry_type=None,
	company=None,
	items=None,
	posting_date=None,
	source_doctype=None,
	source_name=None,
	remarks=None,
) -> dict:
	"""File a Material Receipt, Issue or Transfer. IT COMES BACK A DRAFT.

	`submit_stock_entry` IS NOT ROUTED HERE AND MUST NOT BE. Submitting a Stock
	Entry writes GL entries, and a posting to the general ledger does not
	originate on a handset in a chemical shed. The two tools are separate in the
	registry for that reason and the sidecar publishes only the first, so the
	worst a lost or replayed call can produce is a draft somebody has to look at.

	EVERY WAREHOUSE IN EVERY LINE IS CHECKED AGAINST THE COMPANY BY THE TOOL —
	`_resolve_warehouse` refuses one belonging to another entity — so scoping the
	company here scopes the whole entry, lines included.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")

	inner: dict = {
		"entry_type": str(entry_type or "").strip(),
		"company": entity,
		"items": _receipt_items(items),
	}
	for key, value in (
		("posting_date", posting_date),
		("source_doctype", source_doctype),
		("source_name", source_name),
		("remarks", remarks),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()

	return stock_tools.create_stock_entry(inner).data


# ── 112. start_inspection ────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("start_inspection", mutating=True, limit=guard.WRITE_LIMIT)
def start_inspection(
	user: str,
	template=None,
	location=None,
	location_doctype=None,
	farm_location_gps=None,
	company=None,
	notes=None,
	visit_id=None,
	farm_task=None,
) -> dict:
	"""Open an inspection visit against one template. Writes no compliance record.

	THE METHOD IS NAMED FOR THE WIZARD, NOT FOR THE TOOL. The seeded
	`inspection_session` wizard declares `submit_method: start_inspection` and
	the tool behind it is `start_inspection_session`; the route has to be spelled
	the way the wizard asks for it or the form a worker just filled in posts to a
	404. The mapping lives here, in one line, rather than in a dispatcher.

	`worker` and `foreman` ARE NOT DECLARED. The tool takes both and this wrapper
	forwards neither: the person opening the visit is the caller, and an account
	that could name somebody else would be filing an inspection against a
	colleague. `worker` is set from the authenticated employee below.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")

	inner: dict = {
		"template": str(template or "").strip(),
		"location": str(location or "").strip(),
		"company": entity,
		"worker": _employee(user),
	}
	for key, value in (
		("location_doctype", location_doctype),
		("farm_location_gps", farm_location_gps),
		("notes", notes),
		("visit_id", visit_id),
		("farm_task", farm_task),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()

	return session_tools.start_inspection_session(inner).data


# ── 113. get_payroll_register ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_payroll_register", limit=guard.READ_LIMIT)
def get_payroll_register(
	user: str,
	company=None,
	pay_period=None,
	date_from=None,
	date_to=None,
	include_drafts=None,
) -> dict:
	"""The period's payroll register, for the person who signs the cheques.

	THE HR ROLE IS REQUIRED AND IT IS THE WHOLE GATE ON THIS ONE. Every other
	read on this surface is either the caller's own work or a board a foreman
	needs to do their job; this is what everybody on the farm was paid, name by
	name, and there is no version of it that is a picker's to see. `HR_ROLES` is
	Farm Manager, HR Manager, HR User and System Manager — deliberately NOT
	`DISPATCH_ROLES`, which would put a crew's wages in front of every foreman on
	the site. The seven gates in `guard.endpoint` have already run; this is the
	eighth and it is the one that matters here.

	THE COMPANY SCOPE IS THE CALLER'S OWN. `guard.require_company` refuses an
	entity this account cannot reach, and a register is exactly the read where
	that matters: the holding company's payroll is not readable by naming it.

	`include_drafts` IS FORWARDED because it is a fact about what the caller is
	reconciling — a run that has not been paid belongs in a review and not in a
	bank reconciliation — and the result says which statuses it counted either
	way. Nothing else is: the tool takes no employee filter and neither does
	this, because a register IS the whole crew and a one-person view of it is
	`get_payroll_entry`.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	personnel.require_hr_role()

	inner: dict = {"company": entity}
	for key, value in (
		("pay_period", pay_period),
		("date_from", date_from),
		("date_to", date_to),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()
	if include_drafts is not None:
		inner["include_drafts"] = include_drafts

	data = payroll_tools.get_payroll_register(inner).data
	return {
		"company": data.get("company"),
		"date_from": data.get("date_from"),
		"date_to": data.get("date_to"),
		"pay_period": data.get("pay_period"),
		"statuses_counted": data.get("statuses_counted") or [],
		"payroll_entries": data.get("payroll_entries") or [],
		"payroll_entry_count": data.get("payroll_entry_count"),
		"employees": data.get("employees") or [],
		"totals": data.get("totals") or {},
		"employer_costs": data.get("employer_costs") or {},
		"grand_total_labor_cost": data.get("grand_total_labor_cost"),
		"total_cost_of_employment": data.get("total_cost_of_employment"),
		"total_employee_withholding": data.get("total_employee_withholding"),
	}


# ── 114. render_pay_stub ─────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("render_pay_stub", mutating=True, limit=guard.WRITE_LIMIT)
def render_pay_stub(
	user: str,
	payroll_entry=None,
	employee=None,
	company_address=None,
	overwrite=None,
) -> dict:
	"""Draw one worker's pay stub and hand the phone a URL for it.

	THE SAME SHAPE AS `generate_w4_pdf` AND FOR THE SAME REASON: the app cannot
	render a PDF and should not try, so the server draws it, attaches it
	privately, and returns `file_url` — which is what the handset opens, prints
	and hands over at the end of a period. A stub given out in the field the
	afternoon it is generated is the one a worker actually reads; one that waits
	for somebody to be at a Desk is one they get a fortnight late, if at all.

	THE HR ROLE IS REQUIRED. A pay stub names somebody's wages and every
	deduction taken from them.

	THE EMPLOYEE MUST BE ON THE CALLER'S OWN CREW. `_employee_argument` is what
	scopes it, exactly as it does on the pause pair and on `log_shift_location`
	— an account that can name anybody on the site in a request body could walk
	the payroll one docname at a time, and this is the read where doing so is
	worth the most to whoever tried.

	`show_employer_contributions` IS NOT FORWARDED. Whether a farm shows its own
	FICA and unemployment on a worker's statement is an operator's policy for the
	whole operation, not a checkbox on the handset of whoever happened to print
	it — two workers on one crew getting differently-shaped stubs on the same
	afternoon is a wage-claim exhibit. The MCP surface keeps the argument.

	EVERY REFUSAL IS THE TOOL'S: that the site needs reportlab, that this person
	is not on that run, that a stub is already attached and `overwrite` was not
	passed. None of it is restated here.
	"""
	allowed = guard.require_scope(user)
	personnel.require_hr_role()
	person = _employee_argument(employee, allowed)
	run = guard.require_scoped_doc("Farm Payroll Entry", payroll_entry, "payroll_entry", allowed)

	inner: dict = {"payroll_entry": run, "employee": person}
	if company_address not in (None, ""):
		inner["company_address"] = str(company_address).strip()
	if overwrite is not None:
		inner["overwrite"] = overwrite

	data = payroll_tools.render_pay_stub(inner).data
	return {
		"payroll_entry": data.get("payroll_entry"),
		"employee": data.get("employee"),
		"employee_name": data.get("employee_name"),
		"pay_period_start": data.get("pay_period_start"),
		"pay_period_end": data.get("pay_period_end"),
		"gross_pay": data.get("gross_pay"),
		"total_deductions": data.get("total_deductions"),
		"net_pay": data.get("net_pay"),
		"ytd": data.get("ytd"),
		"file_url": data.get("file_url"),
		"file_name": data.get("file_name"),
		"replaced": data.get("replaced"),
		"note": data.get("note"),
	}


# ── The three compliance reports ─────────────────────────────────────────────
#
# ALL THREE ARE AGGREGATES, WHICH IS WHY THEY ARE HERE AT ALL. Every other read
# on this transport answers a question about one document or one person's own
# work; these answer a question about a whole crew, a whole year or a whole
# season, and they are on the handset because the person who needs them is
# standing in front of the inspector asking. A foreman who can pull the training
# matrix in a shed has answered "is your crew trained" in the room rather than
# by promising to email it.
#
# THE NUMBERING SKIPS 113 AND 114 DELIBERATELY: two other sessions were
# appending to this file at the same time and had taken them. A gap costs a
# reader nothing; two blocks sharing a number costs them the assumption that the
# number identifies a method.
#
# NONE OF THE FOUR DECLARES AN ARGUMENT THAT CHOOSES ITS OWN DENOMINATOR OR ITS
# OWN SUBJECT. `get_osha_300a_summary` takes `total_hours_worked` and
# `average_employees` as a desk override and NEITHER IS DECLARED BELOW, so
# `routes.bind` cannot pass them: the rate a regulator reads is not a figure
# that gets typed into a phone in a field, and a handset that could set the
# denominator could set the rate.


# ── 115. get_training_compliance_report ──────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_training_compliance_report", limit=guard.READ_LIMIT)
def get_training_compliance_report(
	user: str,
	company=None,
	regime=None,
	training_type=None,
	as_of_date=None,
) -> dict:
	"""Every active employee against every curriculum, with the gaps named.

	AN HR READ, GATED LIKE ONE. A training matrix is a personnel document: it
	says by name who has had what and who has had nothing, which is a register a
	perfectly good field credential has no business reading. `require_hr_role`
	is called here as well as one layer down, for the same reason the five
	discipline routes call it — the refusal should happen before the roster is
	read, not after.

	THE ANSWER IS THE TOOL'S, UNCHANGED, AND ITS SHAPE IS DOCUMENTED IN FULL AT
	`tools/training.py::get_training_compliance_report`. Read that block before
	writing a client against this: `requirements` is spelled the same at two
	levels and holds two different shapes — the COLUMN AXIS at the top level, the
	CELLS on a matrix row — which is how a phone came to draw an empty grid for
	months without erroring. v0.106.0 adds `cells` and `statuses` as row aliases
	and `designation` beside `job_title`, all additive, so a client already
	reading the old spellings reads exactly what it read before.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")

	inner: dict = {"company": entity}
	for key, value in (
		("regime", regime),
		("training_type", training_type),
		("as_of_date", as_of_date),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()

	return training_tools.get_training_compliance_report(inner).data


# ── 116. get_osha_300_log ────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_osha_300_log", limit=guard.READ_LIMIT)
def get_osha_300_log(user: str, company=None, year=None) -> dict:
	"""The Form 300 log for one calendar year, one line per recordable case.

	THE DISPATCH GATE, matching `list_accident_reports` and for the same reason:
	creating a report is open to whoever finds somebody on the ground, but the
	INVESTIGATION and its register are somebody's job. A log is the register in
	its most concentrated form — every recordable injury on the operation, named
	— so it takes the same role the register does.
	"""
	guard.require_dispatch_role(user, "Reading the OSHA 300 log")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")

	return accident_tools.get_osha_300_log({"company": entity, "year": year}).data


# ── 117. get_osha_300a_summary ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_osha_300a_summary", limit=guard.READ_LIMIT)
def get_osha_300a_summary(user: str, company=None, year=None) -> dict:
	"""The annual summary and its three rates, for one calendar year.

	`total_hours_worked` AND `average_employees` ARE NOT DECLARED. The tool takes
	both as a desk override for an operation whose payroll lives outside this
	app, and a handset that could supply the denominator could supply the rate —
	which is the number that goes on a posted form. Omitted from the signature,
	they are unreachable through `routes.bind` whatever a body carries, so what
	comes back here is always computed from the shift register or is null.
	"""
	guard.require_dispatch_role(user, "Reading the OSHA 300A summary")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")

	return accident_tools.get_osha_300a_summary({"company": entity, "year": year}).data


# ── 118. get_spray_application_report ────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_spray_application_report", limit=guard.READ_LIMIT)
def get_spray_application_report(
	user: str,
	company=None,
	date_from=None,
	date_to=None,
	block=None,
	product=None,
) -> dict:
	"""Chemical usage over a period, by product and then by block.

	THE DISPATCH GATE. What went onto which ground over a season is the
	operation's pesticide use record, not a worker's own view of their work —
	`get_active_rei` is the read a picker needs and it is already routed. This
	is the one somebody answers a state inspector from.

	`field` AND `item_code` ARE NOT DECLARED even though the tool accepts both as
	aliases. One spelling per argument on this transport: two names for one
	filter is two things a client can get subtly wrong, and the tool's aliases
	exist for callers who came from the Desk's vocabulary rather than for this
	one.
	"""
	guard.require_dispatch_role(user, "Reading the spray application report")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")

	inner: dict = {"company": entity}
	for key, value in (
		("date_from", date_from),
		("date_to", date_to),
		("block", block),
		("product", product),
	):
		if value not in (None, ""):
			inner[key] = str(value).strip()

	return spray_tools.get_spray_application_report(inner).data


# ── Direct deposit: a worker's own bank details, and nobody else's ──────────
#
# THESE THREE TAKE NO `employee` ARGUMENT, and that is the entire security
# design rather than an omission. Every other write in this file that names a
# person takes an Employee docname from the body and checks it with
# `_employee_argument`, which proves the record belongs to an entity the CALLER
# CAN REACH — the right test for onboarding, where a foreman is acting on
# somebody else's record on purpose.
#
# It is the wrong test here. Company scope is shared by everybody enrolled at
# that company, so an `employee` argument checked only that way would let any
# picker with a handset repoint a colleague's wages at their own account. So
# these resolve the subject with `_employee(user)` — the caller's OWN Employee
# record, from their login — and there is no argument that can widen it. A
# foreman who genuinely needs to enter somebody else's details uses the MCP
# tool, which is gated, audited, and not on a phone in an orchard.
#
# The full account number never comes back out. `list_my_bank_accounts` returns
# what every other read of this register returns: the masked last four.


@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_my_bank_accounts", limit=guard.READ_LIMIT)
def list_my_bank_accounts(user: str) -> dict:
	"""Where this worker's own wages are being deposited. Masked."""
	guard.require_scope(user)
	person = _employee(user)
	return ach_tools.get_employee_bank_account({"employee": person}).data


@frappe.whitelist(methods=["POST"])
@guard.endpoint("add_my_bank_account", mutating=True, limit=guard.WRITE_LIMIT)
def add_my_bank_account(
	user: str,
	bank_name=None,
	routing_number=None,
	account_number=None,
	account_type=None,
	allocation_type=None,
	allocation_amount=None,
) -> dict:
	"""A worker enters their own direct deposit details from the handset.

	`employee`, `company` and `status` ARE NOT ACCEPTED. The first is the point
	of the module comment above; the second follows from it, because the company
	is read off the worker's own Employee record and a body that could name one
	would be naming somebody else's entity; and the third because a phone that
	could set an account Inactive could switch a colleague's deposit off if the
	first rule ever slipped. A new account is always Active.

	The prenote is NOT sent from here. Asking the bank to confirm an account is a
	file somebody transmits, and it is batched — `generate_prenote_file` is where
	that happens, and this account will be in the next one.
	"""
	guard.require_scope(user)
	person = _employee(user)

	inner = {
		"employee": person,
		"bank_name": bank_name,
		"routing_number": routing_number,
		"account_number": account_number,
	}
	for key, value in (
		("account_type", account_type),
		("allocation_type", allocation_type),
		("allocation_amount", allocation_amount),
	):
		if value not in (None, ""):
			inner[key] = value

	return ach_tools.create_employee_bank_account(inner).data


@frappe.whitelist(methods=["POST"])
@guard.endpoint("update_my_bank_account", mutating=True, limit=guard.WRITE_LIMIT)
def update_my_bank_account(
	user: str,
	name=None,
	bank_name=None,
	routing_number=None,
	account_number=None,
	account_type=None,
	allocation_type=None,
	allocation_amount=None,
	status=None,
) -> dict:
	"""Correct one of this worker's own accounts, proved to be theirs first.

	`name` IS A DOCNAME FROM THE BODY, so it is checked before it is used — not
	against company scope, which everybody at the company shares, but against the
	caller's own Employee record. A docname belonging to somebody else reads as
	not found rather than as refused, so the register cannot be mapped by
	watching which error comes back.

	`status` IS accepted here and not on the create above: retiring an account
	you own is an ordinary thing to do from a phone when you change banks, and
	the row it applies to has already been proved yours.
	"""
	guard.require_scope(user)
	person = _employee(user)

	docname = str(name or "").strip()
	if not docname:
		frappe.throw("name is required.", frappe.ValidationError)
	owner = frappe.db.get_value("Employee Bank Account", docname, "employee")
	if not owner or str(owner) != person:
		frappe.throw(
			f"No bank account called {docname} belongs to you.",
			frappe.DoesNotExistError,
		)

	inner: dict = {"name": docname}
	for key, value in (
		("bank_name", bank_name),
		("routing_number", routing_number),
		("account_number", account_number),
		("account_type", account_type),
		("allocation_type", allocation_type),
		("allocation_amount", allocation_amount),
		("status", status),
	):
		if value not in (None, ""):
			inner[key] = value

	return ach_tools.update_employee_bank_account(inner).data


# ── 119. list_payroll_deductions ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_payroll_deductions", limit=guard.READ_LIMIT)
def list_payroll_deductions(
	user: str,
	company=None,
	employee=None,
	deduction_type=None,
	deduction_category=None,
	status=None,
	reference=None,
	in_force_on=None,
	limit=None,
) -> dict:
	"""The register of garnishments and voluntary deductions, for this entity.

	WHY A HANDSET NEEDS THIS AT ALL. An office asked "is the support order on
	file yet" has to answer before the run, not after it, and the person who
	knows is frequently not at a desk. The alternative this replaces is somebody
	reading the answer off a Desk session over the phone.

	SCOPED TO THE CALLER'S ENTITIES like every other read here. A support order
	filed against a worker at one company is not visible to another, even where
	the same person works for both — which is also how the withholding behaves.

	IT IS A READ OVER PII AND IS RATE LIMITED AS ONE. What a person's wages are
	garnished for is among the more sensitive facts this app holds; `READ_LIMIT`
	is a foreman answering a question and is not a register being enumerated.

	IT IS `require_hr_role`, SINCE v0.94.0, AND IT WAS NOT BEFORE. Three of the
	five deduction methods on this surface were scope-only reads while the two
	WRITES carried the gate, so the register a garnishment lives in could be read
	by any enrolled account in the entity and only changed by HR. Three separate
	places in this codebase asserted the opposite — that all five were HR-only in
	their own bodies — and were wrong; the gate is here now and those sentences
	are corrected in the same commit.

	IT COSTS THIS FARM NOTHING, WHICH IS WHY IT IS THE ONE TIGHTENING IN A RELEASE
	OF WIDENINGS. `HR_ROLES` already names Farm Manager, so on a farm with no HR
	staff "HR-gated" does not mean calling a department that does not exist — it
	means the farmer, who was always the person doing this. The only account this
	newly excludes is a picker reading a coworker's child-support order.
	"""
	allowed = guard.require_scope(user)
	personnel.require_hr_role()
	inner: dict = {"company": _company(user, company, allowed)}
	if employee:
		inner["employee"] = _employee_argument(employee, allowed)
	for key, value in (
		("deduction_type", deduction_type),
		("deduction_category", deduction_category),
		("status", status),
		("reference", reference),
		("in_force_on", in_force_on),
		("limit", limit),
	):
		if value not in (None, ""):
			inner[key] = value

	return payroll_deduction_tools.list_payroll_deductions(inner).data


# ── 120. get_payroll_deduction ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_payroll_deduction", limit=guard.READ_LIMIT)
def get_payroll_deduction(user: str, deduction=None) -> dict:
	"""One deduction in full, with what the payroll engine will make of it.

	The docname is proved to belong to an entity this caller may reach before it
	is read, so a row belonging to another company reads as not found rather than
	as refused — the register cannot be mapped by watching which error comes back.

	IT IS `require_hr_role`, SINCE v0.94.0, AND IT WAS NOT BEFORE. Three of the
	five deduction methods on this surface were scope-only reads while the two
	WRITES carried the gate, so the register a garnishment lives in could be read
	by any enrolled account in the entity and only changed by HR. Three separate
	places in this codebase asserted the opposite — that all five were HR-only in
	their own bodies — and were wrong; the gate is here now and those sentences
	are corrected in the same commit.

	IT COSTS THIS FARM NOTHING, WHICH IS WHY IT IS THE ONE TIGHTENING IN A RELEASE
	OF WIDENINGS. `HR_ROLES` already names Farm Manager, so on a farm with no HR
	staff "HR-gated" does not mean calling a department that does not exist — it
	means the farmer, who was always the person doing this. The only account this
	newly excludes is a picker reading a coworker's child-support order.

	THE ROLE CHECK RUNS BEFORE `require_scoped_doc`, DELIBERATELY. A refused
	caller must learn nothing about the docname they asked for — not even whether
	it exists — so the order here is the difference between "you may not read
	this register" and an oracle that confirms row names one guess at a time.
	"""
	allowed = guard.require_scope(user)
	personnel.require_hr_role()
	docname = guard.require_scoped_doc(
		PAYROLL_DEDUCTION,
		deduction,
		"deduction",
		allowed,
	)
	return payroll_deduction_tools.get_payroll_deduction({"deduction": docname}).data


# ── 121. list_employee_deductions ────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_employee_deductions", limit=guard.READ_LIMIT)
def list_employee_deductions(
	user: str,
	employee=None,
	in_force_on=None,
	include_inactive=None,
	gross_pay=None,
	statutory_withholding=None,
	pay_frequency=None,
) -> dict:
	"""Everything standing against one worker's pay, in the order it comes out.

	THE CALL AN ONBOARDING OR A PAYROLL QUESTION ACTUALLY MAKES. Sorted the way
	the run will process it — child support, tax levies, student loans, other
	garnishments, then the worker's own elections — so it reads as what will
	happen rather than as rows somebody has to rank.

	`gross_pay` TURNS IT INTO A PRICED PREVIEW against the CCPA ceilings. Send
	`statutory_withholding` with it: disposable earnings is gross less the
	withholding the law requires, and without it the preview treats gross as
	disposable and OVERSTATES what a garnishment may take. The answer says so
	rather than quietly assuming zero tax.

	IT IS `require_hr_role`, SINCE v0.94.0, AND IT WAS NOT BEFORE. Three of the
	five deduction methods on this surface were scope-only reads while the two
	WRITES carried the gate, so the register a garnishment lives in could be read
	by any enrolled account in the entity and only changed by HR. Three separate
	places in this codebase asserted the opposite — that all five were HR-only in
	their own bodies — and were wrong; the gate is here now and those sentences
	are corrected in the same commit.

	IT COSTS THIS FARM NOTHING, WHICH IS WHY IT IS THE ONE TIGHTENING IN A RELEASE
	OF WIDENINGS. `HR_ROLES` already names Farm Manager, so on a farm with no HR
	staff "HR-gated" does not mean calling a department that does not exist — it
	means the farmer, who was always the person doing this. The only account this
	newly excludes is a picker reading a coworker's child-support order.

	THE ROLE CHECK RUNS BEFORE `_employee_argument`, for the reason above: the
	refusal should not depend on, or reveal, whether the named worker resolves.
	"""
	allowed = guard.require_scope(user)
	personnel.require_hr_role()
	inner: dict = {"employee": _employee_argument(employee, allowed)}
	for key, value in (
		("in_force_on", in_force_on),
		("include_inactive", include_inactive),
		("gross_pay", gross_pay),
		("statutory_withholding", statutory_withholding),
		("pay_frequency", pay_frequency),
	):
		if value not in (None, ""):
			inner[key] = value

	return payroll_deduction_tools.list_employee_deductions(inner).data


# ── 122. create_payroll_deduction ────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_payroll_deduction", mutating=True, limit=guard.WRITE_LIMIT)
def create_payroll_deduction(
	user: str,
	employee=None,
	company=None,
	deduction_category=None,
	amount=None,
	deduction_type=None,
	amount_type=None,
	basis=None,
	max_per_period=None,
	priority=None,
	effective_from=None,
	effective_to=None,
	status=None,
	reference=None,
	pre_tax=None,
	fica_exempt=None,
	supports_other_dependents=None,
	arrears_over_12_weeks=None,
	exempt_amount=None,
	label=None,
	notes=None,
) -> dict:
	"""File a garnishment or a voluntary election from the handset.

	THE DAY THIS EXISTS FOR IS THE DAY THE ORDER ARRIVES. A support order is
	served on the employer with a date on it, and withholding is required from
	the first pay period after service — so the gap between "the envelope was
	opened in the yard" and "somebody was at a Desk" is a gap with a liability in
	it. Filing it from where the envelope was opened closes that.

	EVERY REFUSAL THE TOOL MAKES IS MADE HERE TOO, because the tool is what runs:
	a duplicate active order with the same reference, a garnishment marked
	pre-tax, a percentage over 100, a window that ends before it starts, an
	employee name matching more than one person. The employee is proved to be
	inside this caller's entities first, so an order cannot be filed against
	somebody at another company.
	"""
	allowed = guard.require_scope(user)
	inner: dict = {
		"employee": _employee_argument(employee, allowed),
		"company": _company(user, company, allowed),
	}
	for key, value in (
		("deduction_category", deduction_category),
		("amount", amount),
		("deduction_type", deduction_type),
		("amount_type", amount_type),
		("basis", basis),
		("max_per_period", max_per_period),
		("priority", priority),
		("effective_from", effective_from),
		("effective_to", effective_to),
		("status", status),
		("reference", reference),
		("pre_tax", pre_tax),
		("fica_exempt", fica_exempt),
		("supports_other_dependents", supports_other_dependents),
		("arrears_over_12_weeks", arrears_over_12_weeks),
		("exempt_amount", exempt_amount),
		("label", label),
		("notes", notes),
	):
		if value not in (None, ""):
			inner[key] = value

	return payroll_deduction_tools.create_payroll_deduction(inner).data


# ── 123. update_payroll_deduction ────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("update_payroll_deduction", mutating=True, limit=guard.WRITE_LIMIT)
def update_payroll_deduction(
	user: str,
	deduction=None,
	status=None,
	deduction_type=None,
	deduction_category=None,
	amount_type=None,
	amount=None,
	basis=None,
	max_per_period=None,
	priority=None,
	effective_from=None,
	effective_to=None,
	reference=None,
	pre_tax=None,
	fica_exempt=None,
	supports_other_dependents=None,
	arrears_over_12_weeks=None,
	exempt_amount=None,
	label=None,
	notes=None,
) -> dict:
	"""Change a filed deduction, or retire it by setting its status.

	`employee` AND `company` ARE NOT IN THIS SIGNATURE AT ALL, which is the
	strongest form of the tool's own refusal: there is no argument to send. A
	deduction moved to another worker would apply an order made against one
	person to somebody else. Retire this row and file a new one.

	STOPPING A DEDUCTION IS A STATUS CHANGE OR AN END DATE, never a deletion. A
	garnishment removed from the file cannot answer the court that asks why the
	withholding stopped.
	"""
	allowed = guard.require_scope(user)
	docname = guard.require_scoped_doc(
		PAYROLL_DEDUCTION,
		deduction,
		"deduction",
		allowed,
	)

	inner: dict = {"deduction": docname}
	for key, value in (
		("status", status),
		("deduction_type", deduction_type),
		("deduction_category", deduction_category),
		("amount_type", amount_type),
		("amount", amount),
		("basis", basis),
		("max_per_period", max_per_period),
		("priority", priority),
		("effective_from", effective_from),
		("effective_to", effective_to),
		("reference", reference),
		("pre_tax", pre_tax),
		("fica_exempt", fica_exempt),
		("supports_other_dependents", supports_other_dependents),
		("arrears_over_12_weeks", arrears_over_12_weeks),
		("exempt_amount", exempt_amount),
		("label", label),
		("notes", notes),
	):
		if value not in (None, ""):
			inner[key] = value

	return payroll_deduction_tools.update_payroll_deduction(inner).data


# ════════════════════════════════════════════════════════════════════════════
# THE CURRICULUM AND THE AFTERNOON — eight routes, and the one that is not
# gated like the other seven.
#
# `get_training_curriculum` IS OPEN ON ENROLMENT ALONE, and it is the only one
# here that is. It returns what a COURSE is — a video link, a materials list, a
# duration — and none of that is a fact about a person. The picker who has just
# been told by the compliance tab that their WPS card lapsed is exactly who
# should be able to open the film, and a gate that made them ask a foreman for
# it would be a gate that turns a two-minute fix into somebody's Monday.
#
# THE OTHER SEVEN ARE GATED ONE LAYER DOWN, AND SINCE v0.94.0 THE SESSION CALLS
# TAKE `require_shift_role` RATHER THAN `require_hr_role`. A session names by name
# who was taught what, which is a personnel document — but running the tailgate is
# the named supervisor's statutory duty under OAR 437-004-1131, so the gate that
# decides who may hold one is the gate that names supervisors. `update_training_type`
# below is the exception and keeps the HR gate: a curriculum is a decision with a
# citation behind it rather than an afternoon on a block.
#
# THE NUMBERING CONTINUES FROM 123 and may have gaps: several sessions were
# appending to this file at once. A gap costs a reader nothing; two blocks
# sharing a number costs them the assumption that the number identifies a method.


# ── 124. get_training_curriculum ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_training_curriculum", limit=guard.READ_LIMIT)
def get_training_curriculum(user: str, training_type=None, include_inactive=None) -> dict:
	"""What a course is, in the shape a handset renders it.

	NO COMPANY ARGUMENT, and there is nothing missing. A Training Type is a
	site-wide master — 'WPS Handler Training' is the same course whichever entity
	ran it — so there is no per-company scope to apply and inventing one would
	refuse a curriculum on the grounds of an entity it does not belong to.
	"""
	guard.require_scope(user)

	inner: dict = {}
	for key, value in (("training_type", training_type), ("include_inactive", include_inactive)):
		if value not in (None, ""):
			inner[key] = value

	return training_session_tools.get_training_curriculum(inner).data


# ── 125. update_training_type ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("update_training_type", mutating=True, limit=guard.WRITE_LIMIT)
def update_training_type(
	user: str,
	training_type=None,
	video_url=None,
	materials_description=None,
	duration_minutes=None,
	description=None,
	delivery_method=None,
	active=None,
) -> dict:
	"""Put the content on a curriculum, from the phone of whoever ran the session.

	`regimes` AND `retention_years` ARE ON THE TOOL AND NOT IN THIS SIGNATURE, so
	`routes.bind` drops them. Which audits a course answers and how long its
	records are kept are decisions with a citation behind them, made once at a
	desk by somebody who has read the rule — not corrections typed into a phone
	in a shed. Everything here is content: the film, the handouts, the minutes.
	"""
	guard.require_scope(user)
	personnel.require_hr_role()

	inner: dict = {"training_type": training_type}
	for key, value in (
		("video_url", video_url),
		("materials_description", materials_description),
		("duration_minutes", duration_minutes),
		("description", description),
		("delivery_method", delivery_method),
		("active", active),
	):
		if value is not None:
			inner[key] = value

	return training_session_tools.update_training_type(inner).data


# ── 126. create_training_session ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_training_session", mutating=True, limit=guard.WRITE_LIMIT)
def create_training_session(
	user: str,
	training_type=None,
	company=None,
	session_date=None,
	start_time=None,
	end_time=None,
	location=None,
	conducted_by=None,
	instructor_name=None,
	provider=None,
	duration_minutes=None,
	delivery_method=None,
	regimes=None,
	content_topics_covered=None,
	expires_date=None,
	training_source=None,
	notes=None,
) -> dict:
	"""Open a group training event from the shed it is about to happen in.

	`status` IS NOT IN THIS SIGNATURE. A session created from a handset is one
	that is about to run, and the two states worth choosing between are the
	default and In Progress — neither of which is worth an argument a caller
	could get wrong. Completed is refused by the tool anyway.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")

	inner: dict = {"training_type": training_type, "company": entity}
	for key, value in (
		("session_date", session_date),
		("start_time", start_time),
		("end_time", end_time),
		("location", location),
		("conducted_by", conducted_by),
		("instructor_name", instructor_name),
		("provider", provider),
		("duration_minutes", duration_minutes),
		("delivery_method", delivery_method),
		("regimes", regimes),
		("content_topics_covered", content_topics_covered),
		("expires_date", expires_date),
		("training_source", training_source),
		("notes", notes),
	):
		if value not in (None, ""):
			inner[key] = value

	if inner.get("conducted_by"):
		inner["conducted_by"] = _employee_argument(inner["conducted_by"], allowed, "conducted_by")

	return training_session_tools.create_training_session(inner).data


# ── 127. add_session_attendee ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("add_session_attendee", mutating=True, limit=guard.WRITE_LIMIT)
def add_session_attendee(
	user: str,
	session=None,
	badge_scan=None,
	employee=None,
	scan_latitude=None,
	scan_longitude=None,
	scan_accuracy_meters=None,
	scanned_at=None,
	attended=None,
	notes=None,
) -> dict:
	"""Scan somebody in at the shed door.

	THE ROUTE THIS WHOLE SET EXISTS FOR. Everything else here has a desk it could
	have been done from; this one happens with a phone in one hand and a queue of
	people at the door, and it is why the badge — not a typed name — is the
	identification. `resolve_badge` runs one layer down, so a retired card is
	refused at the door rather than discovered in an audit.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(TRAINING_SESSION, session, "session", allowed)

	inner: dict = {"session": name}
	for key, value in (
		("badge_scan", badge_scan),
		("scan_latitude", scan_latitude),
		("scan_longitude", scan_longitude),
		("scan_accuracy_meters", scan_accuracy_meters),
		("scanned_at", scanned_at),
		("attended", attended),
		("notes", notes),
	):
		if value not in (None, ""):
			inner[key] = value
	if employee not in (None, ""):
		inner["employee"] = _employee_argument(employee, allowed)

	return training_session_tools.add_session_attendee(inner).data


# ── 128. sign_session_attendance ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("sign_session_attendance", mutating=True, limit=guard.WRITE_LIMIT)
def sign_session_attendance(
	user: str,
	session=None,
	employee=None,
	badge_scan=None,
	signature=None,
	signature_base64=None,
	file_token=None,
	verification_method=None,
	replace_signature=None,
	device_id=None,
	gps_latitude=None,
	gps_longitude=None,
) -> dict:
	"""Take a worker's signature on the pad they are holding.

	IT GOES THROUGH `collect_form_signature`, the same chain a Form I-9 and a
	W-4 are signed with — the capture is size-limited and sniffed by its magic
	bytes, the badge is resolved against this employer's own register and refused
	when it names somebody other than the person on the row, the session is
	hashed before the mark is written, and a `Signing Evidence` row records who,
	how, on what device and where.

	`signed_at` IS NOT IN THIS SIGNATURE AND CANNOT BE. The shared chain stamps
	the server's clock, so the evidence row and the column it is evidence about
	say the same moment. A signing time a phone could choose is the one field on
	an attestation worth forging.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(TRAINING_SESSION, session, "session", allowed)

	inner: dict = {"session": name}
	for key, value in (
		("signature", signature),
		("signature_base64", signature_base64),
		("file_token", file_token),
		("verification_method", verification_method),
		("badge_scan", badge_scan),
		("replace_signature", replace_signature),
		("device_id", device_id),
		("gps_latitude", gps_latitude),
		("gps_longitude", gps_longitude),
	):
		if value not in (None, ""):
			inner[key] = value
	if employee not in (None, ""):
		inner["employee"] = _employee_argument(employee, allowed)

	return training_session_tools.sign_session_attendance(inner).data


# ── 129. complete_training_session ───────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("complete_training_session", mutating=True, limit=guard.WRITE_LIMIT)
def complete_training_session(
	user: str,
	session=None,
	content_topics_covered=None,
	regimes=None,
	expires_date=None,
	skip_incomplete=None,
	completed_at=None,
) -> dict:
	"""Turn the sheet into training records, standing where the sheet was filled in.

	`skip_incomplete` IS REACHABLE FROM HERE and that is deliberate: the person
	who knows whether the four who did not sign went home is the person holding
	the phone, and the refusal they get without it names exactly who to go and
	find. A desk deciding that an hour later is deciding it with less
	information, not more.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(TRAINING_SESSION, session, "session", allowed)

	inner: dict = {"session": name}
	for key, value in (
		("content_topics_covered", content_topics_covered),
		("regimes", regimes),
		("expires_date", expires_date),
		("skip_incomplete", skip_incomplete),
		("completed_at", completed_at),
	):
		if value not in (None, ""):
			inner[key] = value

	return training_session_tools.complete_training_session(inner).data


# ── 130. get_training_session ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_training_session", limit=guard.READ_LIMIT)
def get_training_session(user: str, session=None) -> dict:
	"""One session's sign-in sheet, and what is still short on it.

	THE HR GATE CAME OFF THIS WRAPPER IN v0.92.2 AND THE REAL ONE IS UNDERNEATH.
	`training_sessions.get_training_session` now runs `require_shift_role` —
	`HR_ROLES` plus Foreman and Crew Leader, the same list `start_shift` and
	`end_shift` take — so a duplicate `require_hr_role` here would have been the
	only thing still refusing a supervisor, and would have refused them AFTER the
	tool had decided they may read it. One gate, in the layer that owns the
	register.

	WHY THE SUPERVISOR IS ON THE LIST is the argument `SHIFT_ROLES` makes for a
	crew shift, and a tailgate session is the same act: the person who holds a
	heat-illness or WPS briefing at the row end is the named supervisor, and one
	who cannot open the sheet afterwards cannot tell whether the four who arrived
	late ever signed it.

	THE ENTITY GATE IS UNTOUCHED AND IS STILL THE ONE DOING MOST OF THE WORK. A
	session at a company this account's User Permissions do not name reads as NOT
	FOUND, both here and again inside the tool.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(TRAINING_SESSION, session, "session", allowed)
	return training_session_tools.get_training_session({"session": name}).data


# ── 131. list_training_sessions ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_training_sessions", limit=guard.READ_LIMIT)
def list_training_sessions(
	user: str,
	company=None,
	training_type=None,
	status=None,
	employee=None,
	conducted_by=None,
	regime=None,
	from_date=None,
	to_date=None,
	limit=None,
) -> dict:
	"""The session register, scoped to the entities this account may reach.

	THE HR GATE CAME OFF THIS WRAPPER IN v0.92.2, for the reason
	`get_training_session` above gives: the tool underneath now takes
	`require_shift_role`, and a supervisor who may hold a session and may read one
	back needs to be able to find it again tomorrow.

	A LISTING IS THE NARROWER READ OF THE TWO. `tools/training_sessions.py` omits
	the attendee rows from a listing and reports the counts instead, so what comes
	back here is what happened, when, under which regime and how many signed —
	not who. `employee=` still runs through `_employee_argument`, so filtering the
	register by a person is refused for an Employee outside the entities this
	account may reach, exactly as it was before.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")

	inner: dict = {"company": entity}
	for key, value in (
		("training_type", training_type),
		("status", status),
		("conducted_by", conducted_by),
		("regime", regime),
		("from_date", from_date),
		("to_date", to_date),
		("limit", limit),
	):
		if value not in (None, ""):
			inner[key] = value
	if employee not in (None, ""):
		inner["employee"] = _employee_argument(employee, allowed)

	return training_session_tools.list_training_sessions(inner).data


# ── Employee self-service: the records a worker may read about themselves ────
#
# FIVE METHODS THAT TAKE NO `employee` ARGUMENT, and that absence is the whole
# design rather than an omission — the same argument the direct deposit block
# above makes, applied to the four records a worker most often needs and has
# never been able to reach from a handset.
#
# Every other read on this surface that names a person takes an Employee docname
# from the body and checks it with `_employee_argument`, which proves the record
# belongs to an entity the CALLER CAN REACH. That is the right test for
# onboarding, where a foreman is working on somebody else's record on purpose. It
# is the wrong test here: company scope is SHARED by everybody enrolled at that
# company, so an `employee` argument checked that way would let any picker with a
# phone read a colleague's withholding elections, their immigration status and
# what they were paid. So these resolve the subject with `_employee(user)` — the
# caller's OWN Employee record, from their login — and there is no argument that
# can widen it.
#
# NONE OF THE FIVE CARRIES THE HR GATE, which is the point of the set rather than
# a relaxation of the surface. The HR-gated routes over the same registers
# already exist — `get_payroll_register`, `render_pay_stub`,
# `get_training_compliance_report`, `list_payroll_deductions`, `get_i9_form` —
# and every one of them answers a question about a CREW or about somebody else. A
# worker asking what they were paid, what they elected on their W-4, what they
# have been trained in and whether their I-9 needs re-examining is asking about
# themselves; a gate that made them ask a foreman for it would put a colleague
# between a worker and their own pay statement, which is the thing the statute
# behind that statement exists to prevent.
#
# WHAT COMES BACK IS PROJECTED, NEVER FORWARDED WHOLE. The tools behind these
# were written for a Desk and return the record in full — `get_i9_form` reports
# every document number on the form and the URL of the scanned page, `get_w4`
# reports the IP the form was signed from. A projection is what keeps this
# surface's answer to "what did I elect" from growing a new field the day
# somebody adds a column to a doctype, which is how a read like this leaks.
#
# THE NUMBERING JUMPS FROM 131 TO 140 ON PURPOSE, leaving room rather than
# claiming it: several sessions append to this file at once, and a gap costs a
# reader nothing where two blocks sharing a number would cost them the assumption
# that the number identifies a method.

#: Most training records `list_my_trainings` hands back. ONE WORKER'S OWN
#: register: a decade of WPS renewals, food-safety refreshers and equipment cards
#: sits comfortably inside this, and a handset that needed more would be asking
#: for a personnel file rather than for a card list.
MY_TRAINING_LIMIT = 100

#: Most payroll runs `list_my_pay_stubs` will OPEN looking for the caller's own
#: slip. A run has to be opened to see who is on it — the slips are a child table
#: and there is no index from a person to the runs they are on — so this bounds
#: the work one call does rather than the answer it gives. Sixty is a little over
#: two years of biweekly runs; a worker asking about a period further back than
#: that passes `year`, which moves the window rather than widening it.
MY_STUB_SCAN_CAP = 60

#: What `get_my_w4` reports off the caller's own active W-4. THE ELECTIONS AND
#: NOTHING ELSE. `signed_ip` is deliberately absent — it is evidence about a
#: signature rather than an election, and it is the sort of column that becomes
#: interesting to somebody who is not the subject. So are `employer_signer_name`
#: and `employer_signed_at`, which are facts about the employer's processing, and
#: `generated_pdf`, which is a private File URL: `generate_w4_pdf` is the route
#: that draws the federal form and it is already published.
MY_W4_FIELDS = (
	"name",
	"tax_year",
	"status",
	"effective_date",
	"filing_status",
	"multiple_jobs",
	"additional_income_from_other_jobs",
	"dependents_under_17_count",
	"dependents_under_17_amount",
	"other_dependents_count",
	"other_dependents_amount",
	"total_dependents_credit",
	"other_income",
	"deductions",
	"extra_withholding_per_period",
	"signed_at",
)

#: What one stub row reports in `list_my_pay_stubs`. THE EMPLOYER'S OWN
#: CONTRIBUTIONS ARE NOT ON THIS LIST and their absence is the same decision
#: `render_pay_stub` makes about `show_employer_contributions`: whether a farm
#: shows its FICA, FUTA and unemployment on a worker's statement is one operator
#: policy for the whole operation, not something that varies by which door the
#: figures were read through.
MY_STUB_FIELDS = (
	"pay_type",
	"total_hours",
	"regular_hours",
	"overtime_hours",
	"piece_units",
	"piece_rate",
	"gross_pay",
	"earned_gross",
	"minimum_wage_makeup",
	"federal_withholding",
	"state_withholding",
	"social_security",
	"medicare",
	"total_deductions",
	"net_pay",
)

#: What one training record reports in `list_my_trainings`. What was taught, when,
#: and when it lapses. `supervisor_reviewed_by`, `supervisor_signed` and the rest
#: of the §112.161(b) review columns are ABSENT: whether the employer completed
#: its own review of a record is a compliance gap in the training matrix — which
#: is `get_training_compliance_report`, and HR-gated — rather than an answer to
#: "what am I current on".
MY_TRAINING_FIELDS = (
	"name",
	"company",
	"training_type",
	"training_source",
	"provider",
	"completed_date",
	"expires_date",
	"one_time",
	"status_now",
	"days_until_expiry",
	"regimes",
	"certificate_attached",
	"trainee_signed",
)

#: What `get_my_i9` reports off the caller's own I-9. STATUS AND DATES, which is
#: what a worker needs, and NONE OF THE DOCUMENT EVIDENCE, which is what an
#: audit needs and what a stolen handset must not carry: every document NUMBER
#: and issuing authority is absent, as are `generated_pdf`, `signed_pdf` and
#: `document_path` — the scanned pages of a passport or a permanent resident
#: card. The titles and expiry dates ARE here, because "your List A document
#: expires in March" is unreadable without saying which document.
#:
#: `ssn_last_four`, `date_of_birth` and the home address are absent for a
#: different reason: they are facts the caller already knows, so returning them
#: buys a phone nothing and costs it something if it is lost. The two signing IPs
#: go for `signed_ip`'s reason on the W-4 above.
MY_I9_FIELDS = (
	"name",
	"status",
	"hire_date",
	"citizenship_status",
	"alien_work_authorization_expiry",
	"section_1_signed_at",
	"section_2_signed_at",
	"verification_date",
	"receipt_pending",
	"receipt_expires_on",
	"list_a_doc_title",
	"list_a_doc_expiry",
	"list_b_doc_title",
	"list_b_doc_expiry",
	"list_c_doc_title",
	"list_c_doc_expiry",
	"retention_until",
	"destruction_eligible_date",
)

#: What one Section 3 entry reports on the caller's own form: that a
#: reverification happened, why, and how long the document behind it runs. The
#: document number, the verifier's name and the signing IP are absent for the
#: reasons `MY_I9_FIELDS` gives.
MY_I9_REVERIFICATION_FIELDS = (
	"reverification_date",
	"reason",
	"document_title",
	"document_expiry",
)

#: Said when a worker has no active W-4. NOT AN ERROR — see `get_my_w4`.
_NO_W4_NOTE = (
	"There is no active W-4 on file for you, so withholding is being calculated at the default "
	"the payroll run applies to an employee who has not filed one. submit_w4 is what files it."
)

#: Said when a worker has no I-9 at all. NOT AN ERROR — see `get_my_i9`.
_NO_I9_NOTE = (
	"There is no I-9 on file for you on this site. Form I-9 is completed with the employer at "
	"hire; ask an operator, who reaches it with create_i9_form."
)


def _attached_stub_urls(wanted: dict) -> dict:
	"""`{payroll entry: file_url}` for the stubs already drawn, in ONE query.

	`wanted` is `{payroll entry: expected file name}`. A stub is attached to the
	Farm Payroll Entry rather than written to a field — a run carries one per
	person and the doctype has one document — so finding a worker's own means
	matching the file NAME as well as the run, and `pay_stub_pdf.file_name_for`
	is asked what that name is rather than this restating the format.

	THE PAIR IS RE-CHECKED AFTER THE FETCH, because the two `in` filters are a
	PRODUCT rather than a set of pairs — every combination of the runs asked
	about and the names asked about comes back. The name filter is what keeps a
	colleague's stub out; the re-check is what keeps the CALLER'S OWN name off
	the wrong run, which would report one period's statement as another's.
	"""
	if not wanted:
		return {}
	rows = (
		frappe.db.get_all(
			"File",
			filters={
				"attached_to_doctype": payroll_tools.PAYROLL_ENTRY,
				"attached_to_name": ("in", sorted(wanted)),
				"file_name": ("in", sorted(set(wanted.values()))),
			},
			fields=["attached_to_name", "file_name", "file_url"],
			limit_page_length=0,
		)
		or []
	)
	found: dict = {}
	for row in rows:
		entry = str(row.get("attached_to_name") or "")
		if wanted.get(entry) == str(row.get("file_name") or "") and row.get("file_url"):
			found[entry] = row.get("file_url")
	return found


def _my_slip(entry: dict, person: str) -> dict:
	"""The one slip on a run that is this caller's, or `{}`.

	The run is read through `get_payroll_entry`, which is a PUBLIC tool returning
	every slip on it, and the filtering happens here. That is deliberate: the
	alternative is reaching into the payroll module's private child-row accessor,
	and a slip's shape would then be restated in two files that have to agree
	about what a payroll figure is called.
	"""
	for slip in entry.get("slips") or []:
		if str(slip.get("employee") or "") == person:
			return dict(slip)
	return {}


# ── 140. get_my_w4 ───────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_my_w4", limit=guard.READ_LIMIT)
def get_my_w4(user: str) -> dict:
	"""What this worker elected on their own W-4. Nobody else's, ever.

	NO ARGUMENTS AT ALL, which is what makes it safe on a surface where company
	scope is shared — see the block above.

	`on_file` IS FALSE RATHER THAN A REFUSAL when there is no active form. The
	tool answers a missing W-4 with a `ToolError`, which `guard.endpoint` turns
	into a validation error carrying `error.validation.failed` — and a phone
	cannot tell that apart from "the server is broken", which are two states with
	completely different buttons on them. "You have not filed one, here is how"
	is the answer this screen exists to give, and it is not an error.

	THE ACTIVE FORM IS THE ONE THIS ANSWERS ABOUT. A superseded W-4 is still
	evidence of what was elected last year and is still on the register; what a
	worker means by "my W-4" is the one payroll is withholding against today.
	"""
	guard.require_scope(user)
	person = _employee(user)

	if not frappe.db.get_value(
		w4.W4_FORM,
		{"employee": person, "status": "Active"},
		"name",
		order_by="tax_year desc, effective_date desc",
	):
		return {"employee": person, "on_file": False, "w4": None, "note": _NO_W4_NOTE}

	data = w4.get_w4({"employee": person}).data
	return {
		"employee": person,
		"on_file": True,
		"w4": {key: data.get(key) for key in MY_W4_FIELDS},
	}


# ── 141. list_my_pay_stubs ───────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_my_pay_stubs", limit=guard.READ_LIMIT)
def list_my_pay_stubs(user: str, year=None) -> dict:
	"""Every payroll run this worker was on, with their own figures off each.

	NOT `get_payroll_register`, WHICH IS THE SAME REGISTER READ THE OTHER WAY.
	That one is company-scoped, HR-gated and reports everybody; this one is one
	person and reports only them, so the two cannot be turned into each other by
	changing an argument — there is no argument to change.

	`company` IS NOT DECLARED EITHER. The runs are filtered to every entity this
	account may reach, which for a picker is the one they work for; naming an
	entity could only ever narrow that, and a worker who has been paid by two
	entities of the same operation wants both statements in one list.

	DRAFT RUNS ARE NOT ON IT. `payroll.REGISTER_STATUSES` is Calculated and
	Submitted, read rather than restated. A draft is a figure somebody is still
	working on, and a worker who saw one and then saw it change would be right to
	think they had been shorted.

	`stub_attached` SAYS WHETHER THE PDF EXISTS YET, and `get_my_pay_stub_pdf` is
	what draws it if it does not. Reporting the URL where there is one means a
	handset showing a list of periods does not have to ask about each of them.

	THE SCAN IS CAPPED AND SAYS SO. Slips are a child table with no index from a
	person to the runs they are on, so this opens runs newest-first until
	`MY_STUB_SCAN_CAP`; `truncated` is true when there were more runs to open,
	which is a fact about the search rather than about the worker's history.
	"""
	allowed = guard.require_scope(user)
	person = _employee(user)

	filters: dict = {
		"company": ("in", allowed),
		"status": ("in", list(payroll_tools.REGISTER_STATUSES)),
	}
	wanted_year = str(year or "").strip()
	if wanted_year:
		if len(wanted_year) != 4 or not wanted_year.isdigit():
			frappe.throw(
				f"year must be a four-digit calendar year, got {wanted_year!r}. Nothing was read.",
				frappe.ValidationError,
			)
		filters["pay_period_end"] = ("between", [f"{wanted_year}-01-01", f"{wanted_year}-12-31"])

	runs = (
		frappe.db.get_all(
			payroll_tools.PAYROLL_ENTRY,
			filters=filters,
			fields=["name"],
			order_by="pay_period_end desc",
			limit_page_length=MY_STUB_SCAN_CAP + 1,
		)
		or []
	)
	truncated = len(runs) > MY_STUB_SCAN_CAP
	runs = runs[:MY_STUB_SCAN_CAP]

	stubs = []
	for row in runs:
		entry = payroll_tools.get_payroll_entry({"name": row["name"]}).data
		slip = _my_slip(entry, person)
		if not slip:
			continue
		stubs.append(
			{
				"payroll_entry": entry.get("name"),
				"company": entry.get("company"),
				"employee": person,
				"employee_name": slip.get("employee_name"),
				"pay_period_start": entry.get("pay_period_start"),
				"pay_period_end": entry.get("pay_period_end"),
				"pay_frequency": entry.get("pay_frequency"),
				"status": entry.get("status"),
				**{key: slip.get(key) for key in MY_STUB_FIELDS},
			}
		)

	attached = _attached_stub_urls(
		{
			stub["payroll_entry"]: pay_stub_pdf.file_name_for(stub)
			for stub in stubs
			if stub.get("payroll_entry")
		}
	)
	for stub in stubs:
		url = attached.get(stub.get("payroll_entry"))
		stub["stub_attached"] = bool(url)
		stub["file_url"] = url

	return {
		"employee": person,
		"year": wanted_year or None,
		"count": len(stubs),
		"pay_stubs": stubs,
		"runs_scanned": len(runs),
		"scan_cap": MY_STUB_SCAN_CAP,
		"truncated": truncated,
		"statuses_counted": list(payroll_tools.REGISTER_STATUSES),
	}


# ── 142. get_my_pay_stub_pdf ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("get_my_pay_stub_pdf", mutating=True, limit=guard.WRITE_LIMIT)
def get_my_pay_stub_pdf(user: str, payroll_entry=None) -> dict:
	"""The itemised statement for one of this worker's own runs, drawn if need be.

	THE STATEMENT ORS 652.610 AND RCW 49.46.020 REQUIRE, reaching the person it
	is about without going through the office. `render_pay_stub` has drawn this
	page since v0.91.0 and is HR-gated, correctly — it takes an `employee` and
	can draw anybody's. This one takes no employee and can only ever draw the
	caller's own.

	IT RETURNS AN ALREADY-ATTACHED STUB RATHER THAN REDRAWING IT, and that is not
	an optimisation. `render_pay_stub` REFUSES a second render unless `overwrite`
	is passed, because the file already there is most likely the statement this
	worker was handed — so a self-service route that called it blindly would fail
	on every period that had already been through the office, which is most of
	them. `overwrite` IS NOT IN THIS SIGNATURE, so `routes.bind` cannot deliver
	it: replacing a statement somebody was handed is a correction, and a
	correction is made by whoever is answerable for the payroll.

	IT IS DECLARED MUTATING, and it is the one method in this self-service set
	that is. A period whose stub has never been drawn is drawn here, which
	attaches a File to the run — a write, on the caller's behalf, of a document
	the law says they are owed. The flag describes what the call can do to the
	site, not who it is for, and `WRITE_LIMIT` follows it: ten a minute is a
	worker fetching statements, not a phone rendering PDFs in a loop.

	A RUN THIS WORKER IS NOT ON READS AS NOT FOUND, in the same words as a run
	that does not exist. `require_scoped_doc` has already made a run belonging to
	another entity unreadable; this closes the remaining half, where a docname
	from a colleague's payslip photograph names a real run at the caller's own
	company. Watching which refusal comes back tells nothing either way.

	NO YEAR-TO-DATE BLOCK COMES BACK IN THE JSON. It is on the page, which is
	where a statement's YTD belongs and where it is labelled as the CALENDAR year
	it is; an envelope that carried it only on the periods that happened to be
	rendered this minute would be a field a client could not rely on.

	v0.92.2 CARRIES THE PDF ITSELF, and until it did this route answered with a
	link nothing could open. `file_url` is a `/private/files/…` path, the funnel
	authenticates with `X-FarmOps-Token` rather than to Frappe, and
	`get_attachment_content` — the door built for exactly that gap — asks Frappe
	whether the caller may read the PARENT. Here the parent is the payroll run,
	which is HR-readable and must stay that way because it holds a slip for every
	person on it. So the one document the law says this worker is owed was the one
	this surface could name and not deliver. `_stub_bytes` says how it delivers it
	and why that is narrower than any permission Frappe can hold.

	THERE IS NO SWITCH TO TURN THE BYTES OFF, and the empty signature is the
	reason. Every argument on one of these five methods is a key `routes.bind`
	will deliver, so a knob costs a permanently wider door for a saving a client
	already has: a statement is one page, and a screen walking a year of periods
	wants `list_my_pay_stubs`, which carries the URL and never the page.
	"""
	allowed = guard.require_scope(user)
	person = _employee(user)
	run = guard.require_scoped_doc(
		payroll_tools.PAYROLL_ENTRY,
		payroll_entry,
		"payroll_entry",
		allowed,
	)

	entry = payroll_tools.get_payroll_entry({"name": run}).data
	slip = _my_slip(entry, person)
	if not slip:
		frappe.throw(f"payroll_entry {run} was not found.", frappe.DoesNotExistError)
	if str(entry.get("status") or "") not in payroll_tools.REGISTER_STATUSES:
		frappe.throw(
			f"Payroll run {run} is {entry.get('status')} rather than "
			f"{' or '.join(payroll_tools.REGISTER_STATUSES)}, so no statement is drawn from it yet. "
			"The figures on a run that has not been calculated are still being worked on. "
			"Nothing was changed.",
			frappe.ValidationError,
		)

	shape_of_answer = {
		"payroll_entry": run,
		"employee": person,
		"employee_name": slip.get("employee_name"),
		"pay_period_start": entry.get("pay_period_start"),
		"pay_period_end": entry.get("pay_period_end"),
		"status": entry.get("status"),
		"gross_pay": slip.get("gross_pay"),
		"total_deductions": slip.get("total_deductions"),
		"net_pay": slip.get("net_pay"),
	}

	file_name = pay_stub_pdf.file_name_for(shape_of_answer)
	existing = _attached_stub_urls({run: file_name}).get(run)
	if existing:
		out = {**shape_of_answer, "file_url": existing, "file_name": file_name, "rendered": False}
		return {**out, **_stub_bytes(run, file_name)}

	data = payroll_tools.render_pay_stub({"payroll_entry": run, "employee": person}).data
	drawn = str(data.get("file_name") or "") or file_name
	out = {
		**shape_of_answer,
		"file_url": data.get("file_url"),
		"file_name": data.get("file_name"),
		"rendered": True,
		"note": data.get("note"),
	}
	return {**out, **_stub_bytes(run, drawn)}


#: The bytes keys, all four of them empty. `_stub_bytes` and `_sealed_bytes` both
#: answer in this shape whatever happens, so a client reads the same keys on the
#: page it got and on the page it did not.
_NO_BYTES = {"encoding": None, "content": None, "content_base64": None, "base64": None}


def _stub_bytes(run: str, file_name: str) -> dict:
	"""One worker's own stub, read back as base64. NEVER RAISES.

	v0.92.2, AND THE FUNNEL IS THE WHOLE REASON. `file_url` on the answer above
	is a `/private/files/…` link; this door authenticates with `X-FarmOps-Token`
	rather than to Frappe, so that link is a login page to the handset holding it.
	The same gap `get_document_preview` was published for, and the same fix: THE
	BYTES TRAVEL IN THE ANSWER, in the three spellings `submit_form_signature`
	and `get_employee_badge_pass` already use.

	`get_attachment_content` CANNOT SERVE THIS ONE, which is why it is here. That
	door asks Frappe whether the caller may read the PARENT, and the parent is the
	payroll run — readable by HR and by nobody else, correctly, because it holds a
	slip for every person on it. The right this worker holds is one file on that
	run, and `read_attached_bytes_unchecked` takes the parent and the file NAME so
	that the check which proves it is theirs stays where it can be made: `run` came
	through `require_scoped_doc`, the employee came from `_employee(user)` and the
	name came from `pay_stub_pdf.file_name_for` — a colleague's stub is a different
	file name on the same run and there is no argument on this signature that
	reaches it.

	NEVER RAISES, for the reason `_sealed_bytes` does not: the statement exists and
	the answer points at it whatever happens in here, and a storage read that failed
	is not a reason to refuse the worker the URL, the period and the net pay.
	"""
	if not run or not file_name:
		return dict(_NO_BYTES)
	try:
		content = file_tools.read_attached_bytes_unchecked(payroll_tools.PAYROLL_ENTRY, run, file_name)
	except Exception:  # pragma: no cover - see the docstring
		return dict(_NO_BYTES)
	if not content:
		return dict(_NO_BYTES)
	encoded = base64.b64encode(content).decode("ascii")
	return {
		"encoding": "base64",
		"content": encoded,
		"content_base64": encoded,
		"base64": encoded,
		"content_type": "application/pdf",
	}


# ── 143. list_my_trainings ───────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_my_trainings", limit=guard.READ_LIMIT)
def list_my_trainings(user: str) -> dict:
	"""What this worker has been trained in, when, and what has lapsed.

	NOT `list_trainings` AND NOT THE TRAINING MATRIX. Both of those are the
	register — they say by name who has had what and who has had nothing, which
	is a personnel document and is why they carry the HR gate. This is one
	person's own card list, which is the thing a picker standing in front of a
	field inspector is asked for.

	`status_now` AND `days_until_expiry` ARE COMPUTED AGAINST TODAY rather than
	read off the stored column, which `training.describe` owns and this does not
	restate: a record saved in March carries March's answer, and a WPS card that
	lapsed last week would still read Active.

	`guard.scoped` RUNS ON THE WAY OUT even though the filter is the caller's own
	Employee docname. It is the belt to that brace and costs one pass: a record
	written against an entity this account cannot reach is a data problem worth
	seeing in the register rather than on a phone.
	"""
	allowed = guard.require_scope(user)
	person = _employee(user)
	today = frappe.utils.today()

	found = training_register.rows({"employee": person}, limit=MY_TRAINING_LIMIT + 1)
	truncated = len(found) > MY_TRAINING_LIMIT
	described = [training_register.describe(row, today) for row in found[:MY_TRAINING_LIMIT]]
	described = guard.scoped(described, allowed)

	records = [{key: row.get(key) for key in MY_TRAINING_FIELDS} for row in described]
	expired = [row["name"] for row in records if row["status_now"] == training_register.STATUS_EXPIRED]
	expiring = [row["name"] for row in records if row["status_now"] == training_register.STATUS_EXPIRING]

	return {
		"employee": person,
		"count": len(records),
		"trainings": records,
		"expired": expired,
		"expiring": expiring,
		"expiring_window_days": training_register.EXPIRING_WINDOW_DAYS,
		"limit": MY_TRAINING_LIMIT,
		"truncated": truncated,
	}


# ── 144. get_my_i9 ───────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_my_i9", limit=guard.READ_LIMIT)
def get_my_i9(user: str) -> dict:
	"""Where this worker's own I-9 stands: status, dates, and what is owed.

	NOT `get_i9_form`, WHICH IS ALREADY ON THIS SURFACE AND ALREADY LETS A WORKER
	READ THEIR OWN. Two things separate them and both matter. That one takes an
	`employee` docname and drops the HR gate only when the docname resolves to
	the caller — so a worker has to name themselves to read themselves, and the
	argument that makes the exception possible is the same argument that carries
	the risk. This one has no argument. And that one returns the record IN FULL:
	every document number, the issuing authorities, and the URLs of the scanned
	passport pages. This returns `MY_I9_FIELDS`, which is the status, the dates
	and the titles — the answer to "am I in order and is anything about to
	expire", which is the question a worker actually has.

	`on_file` IS FALSE RATHER THAN A REFUSAL for `get_my_w4`'s reason: a worker
	whose I-9 was never started needs to be told that in words, not handed a
	validation error a handset will show as a failure.

	`reverification_needed` IS DERIVED FROM THE STATUS rather than being a second
	stored flag. `flag_i9_reverification` raises it and `reverify_i9` lowers it,
	both by moving `status`, so reading anything else here would be reading a
	column that can disagree with the one the workflow moves.

	READING IS LOGGED, INCLUDING THIS READ. `i9.get_i9_form` writes a `Viewed`
	row to the I-9 Audit Log on every call, and a worker looking at their own
	form belongs in that log exactly as much as anybody else does — the log's
	question is who looked, not whether they were entitled to.
	"""
	guard.require_scope(user)
	person = _employee(user)

	if not frappe.db.get_value(i9.I9_FORM, {"employee": person}, "name"):
		return {"employee": person, "on_file": False, "i9": None, "note": _NO_I9_NOTE}

	data = i9.get_i9_form({"employee": person}).data
	form = {key: data.get(key) for key in MY_I9_FIELDS}
	form["reverification_needed"] = str(data.get("status") or "") == "Reverification Needed"
	form["reverification_count"] = data.get("reverification_count")
	form["reverifications"] = [
		{key: row.get(key) for key in MY_I9_REVERIFICATION_FIELDS}
		for row in (data.get("reverifications") or [])
	]
	return {"employee": person, "on_file": True, "i9": form}


# ── 145. get_tax_remittance_summary ──────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_tax_remittance_summary", limit=guard.READ_LIMIT)
def get_tax_remittance_summary(user: str, company=None, fiscal_year=None, quarter=None) -> dict:
	"""What the farm owes every tax authority for a period, on a phone.

	THE HR ROLE IS REQUIRED AND IT IS THE WHOLE GATE, exactly as on
	`get_payroll_register` and for the same reason. This is not one worker's
	figures — it is the whole crew's wages rolled into what the farm remits, and
	there is no version of it that is a picker's to see. `HR_ROLES` is Farm
	Manager, HR Manager, HR User and System Manager, deliberately NOT
	`DISPATCH_ROLES`: a foreman has no business reading the payroll totals of
	every crew on the site.

	THE COMPANY SCOPE IS THE CALLER'S OWN. `guard.require_company` refuses an
	entity this account cannot reach, which matters here for the reason it
	matters on the register — the holding company's tax position is not readable
	by naming it in a request body.

	WHY A PHONE WANTS THIS AT ALL. The person who signs the cheques is not at a
	Desk in the middle of a harvest, and the question "what is going out this
	month" is asked from a truck. The five remittance reads are on this surface
	for that reason and no other; none of them writes anything.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	personnel.require_hr_role()

	data = remittance_tools.get_tax_remittance_summary(_remittance_args(entity, fiscal_year, quarter)).data
	return {
		"company": data.get("company"),
		"tax_year": data.get("tax_year"),
		"quarter": data.get("quarter"),
		"period_start": data.get("period_start"),
		"period_end": data.get("period_end"),
		"federal": data.get("federal") or {},
		"oregon": data.get("oregon") or {},
		"washington": data.get("washington") or {},
		"by_period": data.get("by_period") or [],
		"payroll_entry_count": data.get("payroll_entry_count"),
		"employee_count": data.get("employee_count"),
		"gross_pay": data.get("gross_pay"),
		"grand_total_remittance": data.get("grand_total_remittance"),
		"warnings": data.get("warnings") or [],
	}


# ── 146. get_941_prefill ─────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_941_prefill", limit=guard.READ_LIMIT)
def get_941_prefill(user: str, company=None, fiscal_year=None, quarter=None, deposits=None) -> dict:
	"""Form 941's lines for a quarter, read from a handset.

	`warnings` IS RETURNED WHOLE AND IN ORDER, which matters more here than on
	any other read on this surface: its first entry says that FARMWORKERS BELONG
	ON FORM 943 AND NOT ON THIS FORM. On a tree-fruit operation that is the
	normal case rather than the exception, and a handset that dropped the
	warnings to save bytes would hand somebody a quarterly return for a crew
	whose actual return is annual.

	THE ADJUSTMENT ARGUMENTS ARE NOT FORWARDED. `sick_pay_adjustment`, the
	group-term-life adjustment and the small-business credit are decisions made
	with an accountant against the books, not numbers typed into a phone in a
	orchard — the MCP surface keeps them. `deposits` IS forwarded, because what
	has been paid to EFTPS is a fact the person holding the phone may well be the
	one who knows.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	personnel.require_hr_role()

	inner = _remittance_args(entity, fiscal_year, quarter)
	if deposits not in (None, ""):
		inner["deposits"] = deposits

	data = remittance_tools.get_941_prefill(inner).data
	return {
		"company": data.get("company"),
		"tax_year": data.get("tax_year"),
		"quarter": data.get("quarter"),
		"period_start": data.get("period_start"),
		"period_end": data.get("period_end"),
		"form_941": data.get("form_941") or {},
		"part2_monthly_liability": data.get("part2_monthly_liability") or {},
		"existing_tax_form": data.get("existing_tax_form"),
		"warnings": data.get("warnings") or [],
	}


# ── 147. get_state_tax_remittance ────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_state_tax_remittance", limit=guard.READ_LIMIT)
def get_state_tax_remittance(user: str, company=None, fiscal_year=None, quarter=None, state=None) -> dict:
	"""Oregon's OQ and Form 132, and Washington's ESD report, on a phone.

	`state` IS FORWARDED because a farm that operates on one side of the river
	only should not have to read past the other state's empty report — and one
	that operates on both needs them together, which is the default.

	THE PER-EMPLOYEE DETAIL COMES THROUGH IN FULL. Form 132 and the Washington
	report both name every worker with their wages and hours, and a handset
	summary that dropped the rows would leave the operator unable to check the
	one thing the states reconcile against. That is also why the HR gate is not
	negotiable on this route: these two payloads ARE the crew's wage detail.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	personnel.require_hr_role()

	inner = _remittance_args(entity, fiscal_year, quarter)
	if state not in (None, ""):
		inner["state"] = str(state).strip()

	data = remittance_tools.get_state_tax_remittance(inner).data
	return {
		"company": data.get("company"),
		"tax_year": data.get("tax_year"),
		"quarter": data.get("quarter"),
		"period_start": data.get("period_start"),
		"period_end": data.get("period_end"),
		"states": data.get("states") or [],
		"reports": data.get("reports") or {},
		"due_date": data.get("due_date"),
		"combined_total_due": data.get("combined_total_due"),
		"warnings": data.get("warnings") or [],
	}


# ── 148. get_tax_deposit_schedule ────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_tax_deposit_schedule", limit=guard.READ_LIMIT)
def get_tax_deposit_schedule(
	user: str,
	company=None,
	fiscal_year=None,
	quarter=None,
	lookback_total=None,
	schedule=None,
	payday_offset_days=None,
) -> dict:
	"""Every deposit deadline in a period, with the rule that produced it.

	ALL THREE CORRECTION ARGUMENTS ARE FORWARDED, which is unusual for this
	surface and deliberate. Without them the tool assumes the new-employer
	monthly default and treats each pay period's END as its payday, and both
	assumptions produce dates that are EARLY — safe to be wrong in that
	direction, but not the real deadlines. The person holding the phone is
	usually the one who knows what the farm's lag actually is and what the filed
	941s reported, so this route lets them say rather than making them open a
	Desk to correct a date they are looking at.

	`payday_basis` ON EVERY ROW is passed through untouched. A deadline whose
	provenance is hidden is a deadline somebody will treat as authoritative.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	personnel.require_hr_role()

	inner = _remittance_args(entity, fiscal_year, quarter)
	for key, value in (
		("lookback_total", lookback_total),
		("schedule", schedule),
		("payday_offset_days", payday_offset_days),
	):
		if value not in (None, ""):
			inner[key] = value

	data = remittance_tools.get_tax_deposit_schedule(inner).data
	return {
		"company": data.get("company"),
		"tax_year": data.get("tax_year"),
		"quarter": data.get("quarter"),
		"period_start": data.get("period_start"),
		"period_end": data.get("period_end"),
		"deposit_schedule": data.get("deposit_schedule"),
		"schedule_basis": data.get("schedule_basis"),
		"schedule_assumed": data.get("schedule_assumed"),
		"payday_offset_days": data.get("payday_offset_days"),
		"federal_deposits": data.get("federal_deposits") or [],
		"federal_deposit_total": data.get("federal_deposit_total"),
		"monthly_rollup": data.get("monthly_rollup") or [],
		"state_deadlines": data.get("state_deadlines") or [],
		"futa_liability_in_period": data.get("futa_liability_in_period"),
		"warnings": data.get("warnings") or [],
	}


# ── 149. get_futa_summary ────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_futa_summary", limit=guard.READ_LIMIT)
def get_futa_summary(user: str, company=None, fiscal_year=None, deposits=None) -> dict:
	"""Form 940 for a year, with the two tests that decide it applies at all.

	NO QUARTER ARGUMENT, because Form 940 is annual and the tool refuses one.
	Its quarterly liabilities are computed from the whole year — the $7,000 wage
	base is consumed across it and a quarter cannot see the ones before it — so
	a per-quarter view of this form would be a different and wrong number.

	`agricultural_coverage` IS THE PART TO READ and it comes through whole. FUTA
	does not apply to farm labour unless one of two thresholds is met, and an
	employer under both files no Form 940 at all. Reported, never enforced: the
	tool computes the two figures and a person makes the determination, because
	farm wages paid outside this app count toward both tests and are invisible
	to it.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	personnel.require_hr_role()

	inner = _remittance_args(entity, fiscal_year, None)
	if deposits not in (None, ""):
		inner["deposits"] = deposits

	data = remittance_tools.get_futa_summary(inner).data
	return {
		"company": data.get("company"),
		"tax_year": data.get("tax_year"),
		"period_start": data.get("period_start"),
		"period_end": data.get("period_end"),
		"form_940": data.get("form_940") or {},
		"futa_recorded_on_slips": data.get("futa_recorded_on_slips"),
		"warnings": data.get("warnings") or [],
	}


def _remittance_args(company: str, fiscal_year, quarter) -> dict:
	"""The three arguments every remittance read shares, cleaned the same way.

	One helper rather than five copies because the year is the argument each of
	these refuses without, and five spellings of "strip it and pass it on" is how
	one of them ends up accepting something the other four do not.
	"""
	inner: dict = {"company": company}
	if fiscal_year not in (None, ""):
		inner["fiscal_year"] = str(fiscal_year).strip()
	if quarter not in (None, ""):
		inner["quarter"] = str(quarter).strip()
	return inner


# ── 132. render_training_sign_in_sheet ───────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("render_training_sign_in_sheet", mutating=True, limit=guard.WRITE_LIMIT)
def render_training_sign_in_sheet(user: str, session=None, overwrite=None) -> dict:
	"""Draw the sheet in the shed, while the crew is still in it.

	THE ONE PDF ROUTE ON THIS TABLE THAT IS NOT A READ, and it is here because
	of when it is wanted: a crew leader who has just completed a session is
	standing next to the people who signed it, and "I will print it when I am
	back at the office" is how a sheet gets printed a week later with somebody
	missing from it. The result carries the file_url, so the app can show the
	page it has just made.
	"""
	allowed = guard.require_scope(user)
	name = guard.require_scoped_doc(TRAINING_SESSION, session, "session", allowed)

	inner: dict = {"session": name}
	if overwrite not in (None, ""):
		inner["overwrite"] = overwrite

	return training_session_tools.render_training_sign_in_sheet(inner).data


# ── 133. seal_bin ────────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("seal_bin", mutating=True, limit=guard.UPLOAD_LIMIT)
def seal_bin(
	user: str,
	bin_tag=None,
	bucket_count=None,
	contributors=None,
	shift=None,
	field=None,
	block=None,
	crop=None,
	bucket_session=None,
	gps_lat=None,
	gps_lon=None,
	gps_accuracy_meters=None,
	h3_hex=None,
	sealed_at=None,
	sealed_by=None,
	client_event_id=None,
	nostr_event_id=None,
	notes=None,
) -> dict:
	"""Close one bin in the field, with the names of the people whose buckets are in it.

	THE MOMENT THE ANSWER EXISTS AND THE ONLY ONE. A bin leaves the orchard on a
	trailer carrying a tag and nothing else; the buckets are tipped and mixed, the
	badge scans live on a handset, and every question the packing house will ask
	afterwards — whose fruit, which block, which shift, and therefore which spray
	record and which weather timeline — is a join from that tag back to an hour
	nobody wrote down. This is what writes it down, at the moment the checker taps
	Seal, which is the last instant anybody knows.

	`UPLOAD_LIMIT` RATHER THAN `WRITE_LIMIT`, and for the reason `sync_bucket_entries`
	uses it: a checker seals bins through a morning with no signal and the queue
	drains in a burst when the phone finds the yard's wifi. Ten calls a minute
	would refuse most of a day's harvest, and a refused seal is a bin that arrives
	at the pack line traceable to nobody. `client_event_id` is what makes that
	safe — a retry finds the seal it already wrote and creates nothing.

	`company` IS NOT ON THIS SIGNATURE. It is resolved from the shift, or from the
	checker, and never from a body key: this table's argument filter is what makes
	that unreachable rather than merely refused, and a phone that could name an
	entity would be filing another farm's harvest against this one's crew.

	`source` IS NOT ON IT EITHER, for the same kind of reason. Everything arriving
	here IS the handset, and a key that could say `Manual` would let a phone
	disguise a typed record as a scanned one — the two are different evidence and
	the register has to be able to tell them apart.
	"""
	allowed = guard.require_scope(user)

	inner: dict = {"bin_tag": bin_tag, "bucket_count": bucket_count}
	if shift is not None:
		inner["shift"] = guard.require_scoped_doc(FARM_SHIFT, shift, "shift", allowed)
	if sealed_by is not None:
		inner["sealed_by"] = _employee_argument(sealed_by, allowed, "sealed_by")
	if contributors is not None:
		inner["contributors"] = _bin_contributors(contributors, allowed)
	for key, value in (
		("field", field),
		("block", block),
		("crop", crop),
		("bucket_session", bucket_session),
		("gps_lat", gps_lat),
		("gps_lon", gps_lon),
		("gps_accuracy_meters", gps_accuracy_meters),
		("h3_hex", h3_hex),
		("sealed_at", sealed_at),
		("client_event_id", client_event_id),
		("nostr_event_id", nostr_event_id),
		("notes", notes),
	):
		if value is not None:
			inner[key] = value

	result = bin_seal_tools.seal_bin(inner)
	return result.data


def _bin_contributors(raw, allowed: list) -> list:
	"""The contributor list, with every Employee docname held to the caller's scope.

	A BADGE IS PASSED THROUGH UNCHECKED AND AN EMPLOYEE IS NOT, which looks
	inconsistent and is not. `_employee_argument` is what stops an account naming
	somebody from an entity it cannot see; a badge carries no entity, and the tool
	resolves it against the badge register FOR THIS COMPANY — a card issued by
	another farm resolves to nobody there. So the two arrive at the same guarantee
	by different doors, and checking the badge here would mean resolving it twice
	with two chances to disagree.

	A NON-LIST IS PASSED THROUGH UNTOUCHED so the tool produces the refusal. A
	wrapper that quietly coerced the wrong shape would move the error message away
	from the code that knows what the right shape is.
	"""
	if not isinstance(raw, (list, tuple)):
		return raw
	out = []
	for entry in raw:
		if isinstance(entry, dict) and entry.get("employee"):
			entry = {**entry, "employee": _employee_argument(entry["employee"], allowed, "contributors")}
		out.append(entry)
	return out


# ── 150. register_push_token ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("register_push_token", mutating=True, limit=guard.WRITE_LIMIT)
def register_push_token(
	user: str,
	token=None,
	device_id=None,
	platform=None,
	app_version=None,
	device_model=None,
) -> dict:
	"""The handset says where a break horn should be delivered. Called on every login.

	`employee` AND `user` ARE ABSENT FROM THIS SIGNATURE, which is the whole of
	its security. Both are resolved from the caller's own login, so `routes.bind`
	has nothing to drop and no body can enrol a device against somebody else's
	name — which would be a way to have another worker's break horns, heat
	alerts and dispatch pings delivered to a phone of your choosing. It is the
	same property `add_my_bank_account` has and for the same reason.

	IDEMPOTENT PER DEVICE, because the app calls it on every launch and Apple
	rotates the token underneath it. Same device_id and platform updates the row
	it already has; the register does not grow a row per launch.

	A worker with no Employee record still gets a row. `_employee` would refuse
	the call, and refusing a registration is worse than storing one that no crew
	push will reach yet: the handset has already asked the OS for permission and
	will not ask again, and the row is repaired the moment somebody links the
	Employee record.
	"""
	guard.require_scope(user)

	person = None
	try:
		person = _employee(user)
	except Exception:  # a login with no Employee record yet — see the docstring
		person = None

	inner = {
		"user": user,
		"token": token,
		"device_id": device_id,
		"platform": platform or "ios",
	}
	if person:
		inner["employee"] = person
	for key, value in (("app_version", app_version), ("device_model", device_model)):
		if value not in (None, ""):
			inner[key] = value

	answer = push_tools.register_push_token(inner).data
	row = answer.get("push_token") or {}
	# THE DEVICE TOKEN IS NOT ECHOED BACK, and the shape is written out here
	# rather than passed through for that reason. `guard.strip_secrets` removes
	# every token-shaped key on the way out of this surface — `push_token` and
	# `token` both trip it — so a pass-through would hand the app a dict with
	# holes in it whose names depended on a hint list two files away. The handset
	# does not need the token echoed: it is the thing that just sent it.
	return {
		"registered": True,
		"created": bool(answer.get("created")),
		"rotated": bool(answer.get("token_rotated")),
		"device": {
			"name": row.get("name"),
			"platform": row.get("platform"),
			"device_id": row.get("device_id"),
			"employee": row.get("employee"),
			"is_active": row.get("is_active"),
			"registered_at": row.get("registered_at"),
			"last_used_at": row.get("last_used_at"),
		},
		# Whether this bench can actually deliver anything. False means the p8
		# key has not been configured yet and the app should go on playing its
		# own local tone rather than expecting one to arrive.
		"push_enabled": bool((answer.get("apns") or {}).get("configured")),
	}


# ── 151. unregister_push_token ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("unregister_push_token", mutating=True, limit=guard.WRITE_LIMIT)
def unregister_push_token(user: str, device_id=None, platform=None) -> dict:
	"""Logging out stops the horn reaching this handset. A soft delete.

	`token` IS DELIBERATELY NOT AN ARGUMENT. The device is the identity here, and
	a logout that had to present the current token would fail exactly when it
	matters most — a phone whose token Apple rotated between login and logout
	would go on receiving another shift's break horns forever.

	A device this app has never seen is answered, not refused: a phone logging
	out on a bad signal before its registration ever landed is a normal thing to
	happen, and an error dialog on a screen the worker is already leaving helps
	nobody.
	"""
	guard.require_scope(user)
	answer = push_tools.unregister_push_token(
		{"user": user, "device_id": device_id, "platform": platform or "ios"}
	).data
	return {
		"deactivated": bool(answer.get("deactivated")),
		"found": bool(answer.get("found")),
		"device": {"platform": answer.get("platform"), "device_id": answer.get("device_id")},
	}


# ── 152. submit_app_feedback ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("submit_app_feedback", mutating=True, limit=guard.UPLOAD_LIMIT)
def submit_app_feedback(
	user: str,
	entry_uuid=None,
	client_reference=None,
	screen=None,
	screen_name=None,
	screen_label=None,
	comment=None,
	feedback_text=None,
	was_dictated=None,
	language=None,
	employee=None,
	employee_name=None,
	role=None,
	roles=None,
	designation=None,
	company=None,
	submitted_at=None,
	timestamp=None,
	app_version=None,
	app_build=None,
	device_model=None,
	os_version=None,
	device_id=None,
	screenshot=None,
	screenshot_omitted=None,
) -> dict:
	"""The in-app feedback bubble's one call. Deduplicated on `entry_uuid`.

	`UPLOAD_LIMIT` RATHER THAN `WRITE_LIMIT`, and for the reason
	`sync_bucket_entries` takes it. This route has never existed, so the iOS half
	has been parking every note against a 404 since the bubble shipped — a park
	rather than a failure, retried every six hours forever. The first call that
	answers 200 therefore drains a backlog that may be weeks deep in one burst,
	and ten calls a minute would refuse most of it and send the phone away to try
	again in six hours. The deduplication below is what makes that burst safe
	rather than the rate limit.

	`user` IS ABSENT FROM THIS SIGNATURE and the app sends one anyway. `guard`
	injects the authenticated caller there and `routes.bind` drops the body's
	copy, so the login on the record is the one that was proved. `employee` IS
	declared and is NOT what gets stored as the author: a shared handset is
	normal here, so the app's claim is kept beside the resolved caller rather
	than instead of it — `tools/app_feedback.py` says which column each lands in.

	`screenshot_filename` AND `screenshot_content_type` ARE NOT DECLARED, so
	`bind` drops both. The extension is read off the first bytes and the filename
	is composed from the docname, which is `api/files.py`'s fourth refusal and
	`signatures._sniff`'s whole argument: a caller-supplied name has nowhere to
	land here, and a file called `.jpg` that is not one is worse than no file.

	A COMPANY THIS CALLER CANNOT REACH FALLS BACK RATHER THAN REFUSING. Every
	other write on this surface refuses it, and this one must not: a 403 is a
	note re-queued and re-sent by a handset that will keep sending the same
	company forever, and `company` here is a filter column on a feedback feed
	rather than a fact anything is posted against. The note is filed under the
	caller's own entity and the complaint survives.
	"""
	allowed = guard.require_scope(user)

	try:
		scoped = _company(user, company, allowed)
	except Exception:  # a handset naming an entity this login cannot reach
		scoped = allowed[0] if allowed else ""

	# A login with no Employee record still files a note. `_employee` would
	# refuse the call, and somebody whose record has not been linked yet is
	# exactly the person whose feedback about being onboarded is worth reading.
	person = None
	try:
		person = _employee(user)
	except Exception:
		person = None

	# `roles` is the array the app sends when it sends anything; `role` is the
	# hat that was actually on. The array is a fallback rather than a second
	# column — the feed filters on one value, and a client that sent only the
	# set should still land in that filter rather than under a blank.
	active_role = str(role or "").strip()
	if not active_role and roles:
		listed = roles if isinstance(roles, list) else json.loads(roles or "[]")
		active_role = ", ".join(str(item).strip() for item in listed if str(item or "").strip())

	answer = feedback_tools.submit_app_feedback(
		{
			"entry_uuid": entry_uuid,
			"client_reference": client_reference,
			"screen": screen,
			"screen_name": screen_name,
			"screen_label": screen_label,
			"comment": comment,
			"feedback_text": feedback_text,
			"was_dictated": was_dictated,
			"language": language,
			"caller_user": user,
			"caller_employee": person,
			"employee": employee,
			"employee_name": employee_name,
			"role": active_role,
			"designation": designation,
			"company": scoped,
			"submitted_at": submitted_at,
			"timestamp": timestamp,
			"app_version": app_version,
			"app_build": app_build,
			"device_model": device_model,
			"os_version": os_version,
			"device_id": device_id,
			"screenshot": screenshot,
			"screenshot_omitted": screenshot_omitted,
		}
	).data
	row = answer.get("app_feedback") or {}
	# THE ANSWER IS THE SAME SHAPE WHETHER THIS WROTE ANYTHING OR NOT, because
	# `FeedbackAPI` treats any 2xx as filed and reads a docname out of the reply
	# best-effort. A duplicate that answered differently would be a second code
	# path on the handset for the case that happens most — the interrupted drain.
	return {
		"filed": True,
		"created": bool(answer.get("created")),
		"duplicate": bool(answer.get("duplicate")),
		"name": row.get("name"),
		"entry_uuid": row.get("entry_uuid"),
		"screen": row.get("screen_name"),
		"submitted_at": row.get("timestamp"),
		"received_at": row.get("received_at"),
		"screenshot_stored": bool(answer.get("screenshot_stored")),
		"screenshot_omitted": answer.get("screenshot_omitted"),
	}


# ── 153. materialize_task_for_alert ──────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("materialize_task_for_alert", mutating=True, limit=guard.WRITE_LIMIT)
def materialize_task_for_alert(
	user: str,
	alert=None,
	urgency=None,
	assigned_to=None,
	employee=None,
	company=None,
) -> dict:
	"""One compliance alert, one Farm Task, handed to one person. v0.106.0.

	THE ROUTE THE APP HAS BEEN CALLING AND GETTING A 404 FROM. `MobileAPI.swift`
	has named `materialize_task_for_alert` since the compliance-to-task feature
	shipped, `ComplianceAPI.createTaskFromAlert` tries it first on every raise,
	and until this release the sidecar had no such path — so every task raised
	from an alert went the long way round: `create_farm_task` with a title, a
	type and a notes blob the HANDSET composed out of the alert's prose, followed
	by `assign_farm_task` when the created task came back holding nobody.

	WHY THE LONG WAY ROUND IS WORSE, WHICH IS THE ARGUMENT FOR THIS EXISTING.
	The fallback's task is not linked to the alert. `source_alert` is not on
	`create_farm_task`'s signature and deliberately so — it is in the list of
	five arguments that door does not take — so the task a foreman raised and the
	alert it answers are two records with no edge between them. Nothing closes
	the alert when the task is completed, `linked_task` on the calendar row stays
	empty, and the next sweep raises the same alert again beside the task
	somebody is already holding. It also re-decides server-side facts on the
	handset: the task type comes off a Swift switch on the alert's title rather
	than off the rule's recipe, and the evidence contract is whatever the app
	guessed rather than what the compliance rule requires.

	`rectify_alert` IS NOT THIS AND STAYS. That route asks the alert what its fix
	IS and refuses one whose fix is a form rather than a task — it is the
	"tap the alert, do what it says" door, and it takes no assignee because
	the rectification decides everything. This is the "hand this to Ana with High
	urgency" door: it takes the two decisions a person standing in an orchard
	makes, and it does not consult `describe_rectification` at all, because a
	foreman may legitimately raise a task for an alert whose canonical fix is a
	desk form somebody else will file later.

	FOREMAN AND ABOVE, exactly as `create_farm_task` is, and for the same reason:
	this raises work onto somebody else's list. `guard.require_dispatch_role`.

	THE ANSWER IS A `FarmTask`, not a report, because the app decodes one. The
	three keys beside it — `alert`, `already_answered`, `routing_notes` — are
	additive and a client that ignores them is not wrong. `already_answered` is
	the one worth reading: this is IDEMPOTENT, so a second tap on the same alert
	returns the task the first tap raised rather than a second task, and a client
	that reported "created" both times would be lying to the person tapping.
	"""
	guard.require_dispatch_role(user, "Raising a farm task from a compliance alert")
	allowed = guard.require_scope(user)
	# `company` is declared and NOT forwarded to the tool, which is not an
	# oversight. The task's entity is the ALERT's — it is the alert's own column
	# and the recipe's — and a body naming a different one would be asking for a
	# task about one farm's cabin to be filed against another farm. Declaring it
	# keeps `routes.bind` from dropping a key the app already sends, and running
	# it through `require_company` keeps the argument meaning what it means
	# everywhere else on this surface: an entity this caller reaches.
	guard.require_company(user, company, allowed)
	name = guard.require_scoped_doc(ALERT, alert, "alert", allowed)

	inner: dict = {"alert": name}
	if urgency not in (None, ""):
		inner["urgency"] = str(urgency).strip()
	person, label = _one_spelling(assigned_to, employee, "assigned_to", "employee")
	if person:
		inner["assigned_to"] = _employee_argument(person, allowed, label)

	data = dispatch.materialize_task_for_alert(inner).data
	docname = str(data.get("task") or "")
	# THE TOOL ANSWERS A REPORT AND THE APP DECODES A TASK, so the task is read
	# back and shaped here rather than the report's keys being renamed into
	# something task-shaped. `shape.task` is what every other task on this
	# surface goes through, and a second, hand-built spelling of a Farm Task is
	# exactly how `completed_at` went missing for four releases.
	out: dict = {}
	if docname:
		row = dispatch.get_farm_task({"task": docname}).data
		out = shape.task(row, row.get("live_assignment") or {})
	out["alert"] = name
	out["already_answered"] = bool(data.get("already_answered"))
	if data.get("routing_notes"):
		out["routing_notes"] = data["routing_notes"]
	if data.get("cohort_note"):
		out["cohort_note"] = data["cohort_note"]
	return out


# ── 154. list_certifications ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_certifications", limit=guard.READ_LIMIT)
def list_certifications(
	user: str,
	company=None,
	cert_type=None,
	status=None,
	holder=None,
	expiring_only=None,
	limit=None,
) -> dict:
	"""The certificate and licence register, soonest expiry first. v0.106.0.

	WHAT A PHONE WANTS IT FOR IS NOT THE REGISTER, IT IS THE HOLDER LIST. The
	compliance-to-task sheet asks "who may I hand a pesticide job to", and the
	honest answer is "whoever holds a current applicator licence" — which lives
	here and nowhere else. Until this release the app had to infer it from the
	training matrix, which answers a DIFFERENT question: a training record says
	somebody sat through the course, and a certificate says the state issued them
	a licence. On this farm those are not the same set of people.

	THE DISPATCH GATE, matching `list_farm_task_templates` rather than the
	unguarded field reads. A certificate register names people and says which of
	them is out of compliance, which is a personnel fact — the picker who holds
	the phone is entitled to their own tasks, not to a list of everybody whose
	licence has lapsed.

	`expiring_only` IS FORWARDED AND `expired` IS NOT A FILTER. The tool reports
	`expired` off the DATE rather than off the status column, because nothing
	rewrites a status when a date passes; a client filtering on `status` would
	show a lapsed licence as Active. Read `expired` and `inside_renewal_window`
	on each row.
	"""
	guard.require_dispatch_role(user, "Reading the certificate register")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")

	inner: dict = {"company": entity}
	for key, value in (
		("cert_type", cert_type),
		("status", status),
		("holder", holder),
		("expiring_only", expiring_only),
		("limit", limit),
	):
		if value not in (None, ""):
			inner[key] = value

	data = evidence_tools.list_certifications(inner).data
	data["certifications"] = guard.scoped(data.get("certifications") or [], allowed)
	data["certification_count"] = len(data["certifications"])
	return data


# ── 155. get_certification ───────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_certification", limit=guard.READ_LIMIT)
def get_certification(user: str, certification=None, company=None) -> dict:
	"""One certificate, with every lapse in its history. v0.106.0.

	THE DETAIL BEHIND A ROW OF `list_certifications`, and the lapse history is
	why it is a separate call rather than more columns on the list: a certificate
	renewed late has a period during which it was not a defence, the register
	keeps that visible on purpose, and it is several rows per certificate.

	SCOPED THE SAME WAY EVERY OTHER DOCNAME ON THIS SURFACE IS. A certificate
	belonging to an entity this account cannot reach answers "not found" rather
	than "refused", so guessing docnames cannot be used to learn whether another
	farm holds a particular licence.
	"""
	guard.require_dispatch_role(user, "Reading a certificate")
	allowed = guard.require_scope(user)
	guard.require_company(user, company, allowed)
	name = guard.require_scoped_doc(CERTIFICATION, certification, "certification", allowed)

	return evidence_tools.get_certification({"certification": name}).data


# ══════════════════════════════════════════════════════════════════════════════
# v0.113.0 — THE REST OF THE LOCATION REGISTER, AND THE ORG CHART
#
# Two things, and they are here together because they are the same shape of gap:
# a register the Desk can maintain and a handset could only ever add to.
#
# ITEM 11 CREATED A PLACE AND COULD NEVER CORRECT ONE. v0.98.0 mounted the read
# and five creates; `routes.py` said at the time that "the three `update_*` tools
# are DELIBERATELY ABSENT" because moving a title is a desk act. That sentence
# was right about `convey_parcel` and wrong about the acreage: the block created
# at six in the morning with the acreage guessed is corrected by the person who
# guessed it, standing on it, and until now the only door was a Desk. Worse, a
# block created TWICE could not be removed at all — by anybody, from anywhere, on
# any transport — because nothing in this app has ever deleted a register row.
#
# THE ORG MASTERS HAD FIFTEEN TOOLS AND NO ROUTES. `tools/org.py` has shipped
# create/list/update for Designation, Department, Branch, Employment Type and
# Employee Grade since it was written, and `create_employee` refuses a value that
# names none of them — so the hiring wizard's Assignment step could offer the
# site's five designations and, the moment the farm hired its first mechanic,
# had no way to add a sixth. `list_onboarding_reference_data` reads the four
# dropdowns and is the wizard's own call; it cannot write, and there was no
# second call that could.
# ══════════════════════════════════════════════════════════════════════════════


# ── 156. update_farm_location ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("update_farm_location", mutating=True, limit=guard.WRITE_LIMIT)
def update_farm_location(
	user: str,
	name=None,
	doctype=None,
	register=None,
	company=None,
	acres=None,
	crop=None,
	variety=None,
	block_number=None,
	condition=None,
	county=None,
	state=None,
	address=None,
	unit_type=None,
	capacity=None,
	water_source=None,
	flow_rate_gpm=None,
	notes=None,
) -> dict:
	"""Correct one place the handset can already create. The other half of item 11.

	THE SHEET IS `CreateLocationSheet` REOPENED. Every argument here is one the
	create sheet already collects, which is deliberate: the screen that made the
	mistake is the screen that fixes it, and a caller who could name a value on
	the way in should not need a different vocabulary to change it.

	THE GATE IS `guard.require_location_role` — Farm Manager, the same gate as the
	five creates and narrower than dispatch. Correcting a register entry reaches
	as far as creating one: every task, spray record and acre of cost allocation
	routes through the docname, and the acreage is what per-acre costing divides
	by.

	THE ENTITY IS PROVED BEFORE THE WRITE, by `_scoped_location` rather than by
	`guard.require_scoped_doc` — all four registers spell the owning entity
	`owning_entity` and that check reads `company`, so it would have passed every
	docname on the bench. The same hand-made check the creates make about a
	parent, made here about the record itself.

	`tools/locations.update_farm_location` DOES THE WRITE, which means the
	register's own `update_` tool does: the parcel acreage rule, the zone-number
	clash, the derived `organic_certified`, the GPS pair that moves together. An
	argument the chosen register has no column for is refused BY NAME rather than
	dropped.

	NOTHING HERE RENAMES ANYTHING. All four registers build the docname from the
	name column and all four tools refuse to re-key; `name` on this signature
	IDENTIFIES the record, it does not set anything.
	"""
	wanted, label = _one_spelling(doctype, register, "doctype", "register")
	chosen = _location_register(wanted, label)
	guard.require_location_role(user, f"Correcting a {chosen} in the location register")
	allowed = guard.require_scope(user)
	guard.require_company(user, company, allowed)

	docname = _scoped_location(chosen, name, "name", allowed)
	inner: dict = {"doctype": chosen, "name": docname}
	for key, value in (
		("acres", acres),
		("crop", crop),
		("variety", variety),
		("block_number", block_number),
		("condition", condition),
		("county", county),
		("state", state),
		("address", address),
		("unit_type", unit_type),
		("capacity", capacity),
		("water_source", water_source),
		("flow_rate_gpm", flow_rate_gpm),
		("notes", notes),
	):
		if value is not None:
			inner[key] = value

	return location_tools.update_farm_location(inner).data


# ── 157. delete_farm_location ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("delete_farm_location", mutating=True, limit=guard.WRITE_LIMIT)
def delete_farm_location(
	user: str, name=None, doctype=None, register=None, company=None, dry_run=None
) -> dict:
	"""Remove the duplicate somebody created ten minutes ago. Nothing else.

	THE ONLY IRREVERSIBLE ROUTE ON THIS SURFACE, and the reason it is here rather
	than left to a Desk is that the duplicate is MADE here: the create routes shipped
	in v0.98.0 put a location register in the hands of everybody with the role, and a
	register that can be added to at a tailgate and only tidied at a desk fills up
	with "Block 3" and "Block 3 " forever.

	THE FOUR SAFETY CHECKS ARE THE TOOL'S AND NOTHING IS RELAXED FOR THE PHONE. A
	place with a child register on it, a record linking to it, ANY task, spray
	record, observation or inspection naming it, or a file attached to it, is
	refused with the count and up to eight examples. In practice that means this
	route can only ever remove a row nothing has touched — which is exactly the
	row it exists for, and every other row is one the register is FOR.

	THE `force_check_…` FLAGS ARE ABSENT FROM THIS SIGNATURE, so `bind` drops
	them and no body can turn a check off. They are on the MCP tool, where an
	operator with the Desk open can decide to skip one; a handset gets the four
	checks or nothing, because the flag that matters — `force_check_activity` —
	turns off the ONE check Frappe's own link integrity does not make.

	`dry_run` IS DECLARED AND IS THE CALL TO MAKE FIRST. It runs all four and
	writes nothing, so the app can grey out its own delete button with the real
	answer rather than guessing at one.

	THE GATE IS `guard.require_location_role` and the entity is proved by
	`_scoped_location`, both for the reasons `update_farm_location` above states.
	"""
	wanted, label = _one_spelling(doctype, register, "doctype", "register")
	chosen = _location_register(wanted, label)
	guard.require_location_role(user, f"Removing a {chosen} from the location register")
	allowed = guard.require_scope(user)
	guard.require_company(user, company, allowed)

	docname = _scoped_location(chosen, name, "name", allowed)
	inner: dict = {"doctype": chosen, "name": docname}
	if dry_run is not None:
		inner["dry_run"] = dry_run
	return location_tools.delete_farm_location(inner).data


# ══════════════════════════════════════════════════════════════════════════════
# THE FIVE ORG MASTERS. Fifteen routes, three shared bodies, one gate each way.
#
# THE READS ARE OPEN ON ENROLMENT and the writes are HR. That split is
# `list_onboarding_reference_data`'s and is argued there at length: a job title,
# a camp name and an employment class are not facts about a person, and the
# wizard has to be able to OFFER the list `create_employee` is about to refuse a
# value against. What each read adds beyond that call is the live headcount,
# which is an aggregate and is the column that makes "is this safe to rename"
# answerable — `list_designations` reports it and `unused` beside it.
#
# THE WRITES CARRY `personnel.require_hr_role` IN THE WRAPPER as well as in the
# tool. Two locks on the same door on purpose: the wrapper's runs before anything
# is read and is what the audit row records, and the tool's is what protects the
# MCP transport, where there is no wrapper.
#
# THE PAY COLUMNS ON Employee Grade ARE ABSENT FROM EVERY SIGNATURE HERE, which
# makes them unreachable rather than merely refused — `bind` delivers only what a
# signature names. `tools/org._refuse_grade_pay` refuses them by name anyway, so
# a caller who finds another way in still gets the sentence rather than a write.
# `default_base_pay` sets what an entire BAND of people is paid.
#
# `delete_` IS ABSENT FOR ALL FIVE and is not an oversight: none of the five
# registers has a delete tool at all. Frappe HR's own answer for a master nobody
# should pick any more is `disabled` on Department and a rename on the rest, and
# `update_` carries both.
# ══════════════════════════════════════════════════════════════════════════════


def _org_list(user: str, doctype: str, company, limit, in_use_only) -> dict:
	"""The shared body of the five org reads. Scope, then the tool.

	`company` NARROWS ONLY Department, which is the one of the five that carries
	one on a stock Frappe HR — the same asymmetry `_onboarding_reference_data`
	handles, and for the same reason a Designation is a job title rather than a
	company's job title.
	"""
	allowed = guard.require_scope(user)
	wanted = guard.require_company(user, company, allowed)
	master = org_tools.BY_DOCTYPE[doctype]

	inner: dict = {}
	if wanted and master.company_scoped:
		inner["company"] = wanted
	if limit not in (None, ""):
		inner["limit"] = limit
	if in_use_only is not None:
		inner["in_use_only"] = in_use_only
	return org_tools._list(master, inner).data


def _org_create(user: str, doctype: str, given: dict, company=None) -> dict:
	"""The shared body of the five org creates. HR gate, entity scope, tool.

	THE GATE RUNS BEFORE ANYTHING IS READ. A register entry is what
	`create_employee` validates against and what a Position Wage Default is keyed
	on, so adding one creates the row a wage rate can hang off — which is a
	personnel change however small the record looks.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	master = org_tools.BY_DOCTYPE[doctype]

	inner = {key: value for key, value in given.items() if value not in (None, "")}
	if master.company_scoped:
		inner["company"] = _company(user, company, allowed)
	return org_tools._create(master, inner).data


def _org_update(user: str, doctype: str, given: dict, company=None) -> dict:
	"""The shared body of the five org updates, renames included.

	A RENAME MOVES EVERY Link ON THE SITE and the tool says so in its own answer:
	`frappe.rename_doc` repoints every Employee already carrying the old title, and
	it is REFUSED where the target name exists, because folding two registers into
	one is a decision about which of them the people on both actually hold.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	master = org_tools.BY_DOCTYPE[doctype]

	inner = {key: value for key, value in given.items() if value is not None}
	if master.company_scoped and company not in (None, ""):
		inner["company"] = _company(user, company, allowed)
	return org_tools._update(master, inner).data


# ── 158. list_designations ───────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_designations", limit=guard.READ_LIMIT)
def list_designations(user: str, company=None, limit=None, in_use_only=None) -> dict:
	"""Every job title on this site, with how many people hold each."""
	return _org_list(user, org_tools.DESIGNATION, company, limit, in_use_only)


# ── 159. create_designation ──────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_designation", mutating=True, limit=guard.WRITE_LIMIT)
def create_designation(user: str, name=None, designation_name=None, description=None) -> dict:
	"""Add the job title the hiring wizard is about to refuse a value against.

	BOTH SPELLINGS OF THE NAME, because `bind` drops what a signature does not
	name and a method taking one of them would be a silent empty column for
	whichever caller guessed wrong. The same call every create on this surface
	makes.
	"""
	return _org_create(
		user,
		org_tools.DESIGNATION,
		{"designation_name": name or designation_name, "description": description},
	)


# ── 160. update_designation ──────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("update_designation", mutating=True, limit=guard.WRITE_LIMIT)
def update_designation(user: str, designation=None, name=None, description=None, new_name=None) -> dict:
	"""Correct one job title's description, or its spelling."""
	return _org_update(
		user,
		org_tools.DESIGNATION,
		{"designation": designation or name, "description": description, "new_name": new_name},
	)


# ── 161. list_departments ────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_departments", limit=guard.READ_LIMIT)
def list_departments(user: str, company=None, limit=None, in_use_only=None) -> dict:
	"""Every department, narrowed to one company, with its headcount."""
	return _org_list(user, org_tools.DEPARTMENT, company, limit, in_use_only)


# ── 162. create_department ───────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_department", mutating=True, limit=guard.WRITE_LIMIT)
def create_department(
	user: str, name=None, department_name=None, company=None, parent_department=None, is_group=None
) -> dict:
	"""Add one department to a company's org chart.

	THE ONE OF THE FIVE WHOSE DOCNAME IS NOT WHAT YOU TYPED. Frappe HR's
	controller appends the company abbreviation, so "Harvest" at Orchard Meadow
	becomes "Harvest - OML" — the answer reports both, and every other tool here
	resolves either.
	"""
	return _org_create(
		user,
		org_tools.DEPARTMENT,
		{
			"department_name": name or department_name,
			"parent_department": parent_department,
			"is_group": is_group,
		},
		company=company,
	)


# ── 163. update_department ───────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("update_department", mutating=True, limit=guard.WRITE_LIMIT)
def update_department(
	user: str,
	department=None,
	name=None,
	company=None,
	parent_department=None,
	is_group=None,
	disabled=None,
	new_name=None,
) -> dict:
	"""Move a department in the tree, retire it, or correct its spelling.

	`disabled` IS THE RETIREMENT AND A RENAME IS NOT. Disabling keeps every
	Employee already in it pointing at it and takes it out of the pickers; that is
	the right answer for a department that was real and is finished.
	"""
	return _org_update(
		user,
		org_tools.DEPARTMENT,
		{
			"department": department or name,
			"parent_department": parent_department,
			"is_group": is_group,
			"disabled": disabled,
			"new_name": new_name,
		},
		company=company,
	)


# ── 164. list_branches ───────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_branches", limit=guard.READ_LIMIT)
def list_branches(user: str, company=None, limit=None, in_use_only=None) -> dict:
	"""Every branch on this site, with how many people are posted to each.

	A BRANCH IS THE CAMP, which is what makes this more than a dropdown:
	`Parcel.branch` is the only column joining an operating unit to the ground its
	housing stands on, and `list_onboarding_reference_data` walks it to turn the
	wizard's Assignment step into its Housing step.
	"""
	return _org_list(user, org_tools.BRANCH, company, limit, in_use_only)


# ── 165. create_branch ───────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_branch", mutating=True, limit=guard.WRITE_LIMIT)
def create_branch(user: str, name=None, branch=None) -> dict:
	"""Add one operating unit or camp. A Branch carries nothing but its name."""
	return _org_create(user, org_tools.BRANCH, {"branch": name or branch})


# ── 166. update_branch ───────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("update_branch", mutating=True, limit=guard.WRITE_LIMIT)
def update_branch(user: str, branch=None, name=None, new_name=None) -> dict:
	"""Correct a branch's spelling. There is nothing else on one to change.

	WHICH IS WHY THIS ROUTE EXISTS AT ALL. "Mill Creak" typed at six in the
	morning is on every Employee posted there since, and `new_name` goes through
	`frappe.rename_doc`, which repoints all of them.
	"""
	return _org_update(user, org_tools.BRANCH, {"branch": branch or name, "new_name": new_name})


# ── 167. list_employment_types ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_employment_types", limit=guard.READ_LIMIT)
def list_employment_types(user: str, company=None, limit=None, in_use_only=None) -> dict:
	"""Every employment category, with how many people are on each."""
	return _org_list(user, org_tools.EMPLOYMENT_TYPE, company, limit, in_use_only)


# ── 168. create_employment_type ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_employment_type", mutating=True, limit=guard.WRITE_LIMIT)
def create_employment_type(user: str, name=None, employment_type_name=None) -> dict:
	"""Add one employment category — Hourly, Seasonal Worker, H-2A.

	THE INSTALLER SEEDS TWO and a farm running an H-2A programme needs a third
	before its first petition worker is hired. This is the route that adds it.
	"""
	return _org_create(
		user, org_tools.EMPLOYMENT_TYPE, {"employment_type_name": name or employment_type_name}
	)


# ── 169. update_employment_type ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("update_employment_type", mutating=True, limit=guard.WRITE_LIMIT)
def update_employment_type(user: str, employment_type=None, name=None, new_name=None) -> dict:
	"""Correct an employment category's spelling. It carries nothing else."""
	return _org_update(
		user, org_tools.EMPLOYMENT_TYPE, {"employment_type": employment_type or name, "new_name": new_name}
	)


# ── 170. list_employee_grades ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_employee_grades", limit=guard.READ_LIMIT)
def list_employee_grades(user: str, company=None, limit=None, in_use_only=None) -> dict:
	"""Every grade on this site, with how many people are on each.

	THE LABEL AND THE HEADCOUNT, AND NOT THE PAY. `tools/org._describe` reports
	the name and `active_employees`; a grade's `default_base_pay` is not among
	the columns it reads, so what a band is paid does not travel to a handset
	even as a read.
	"""
	return _org_list(user, org_tools.EMPLOYEE_GRADE, company, limit, in_use_only)


# ── 171. create_employee_grade ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("create_employee_grade", mutating=True, limit=guard.WRITE_LIMIT)
def create_employee_grade(user: str, name=None, employee_grade_name=None) -> dict:
	"""Add one pay band's LABEL. The pay on it is set in the Desk.

	PROMPT-NAMED, which is the trap this register carries: there is no column
	behind the docname on a stock site, so the name given IS the docname. The tool
	sets both spellings Frappe's insert path may take, rather than trusting
	whichever one this version uses.
	"""
	return _org_create(user, org_tools.EMPLOYEE_GRADE, {"employee_grade_name": name or employee_grade_name})


# ── 172. update_employee_grade ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("update_employee_grade", mutating=True, limit=guard.WRITE_LIMIT)
def update_employee_grade(user: str, employee_grade=None, name=None, new_name=None) -> dict:
	"""Correct a grade's spelling. Its pay columns are absent from this signature."""
	return _org_update(
		user, org_tools.EMPLOYEE_GRADE, {"employee_grade": employee_grade or name, "new_name": new_name}
	)


# ── 173. get_map_overlays ────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_map_overlays", limit=guard.READ_LIMIT)
def get_map_overlays(user: str, company=None, blocks=None, layers=None, limit=None) -> dict:
	"""The operational map, on the phone that is standing in the block. v0.116.0.

	THE MIRROR OF `/app/farm-overview`'s LAYER PICKER, and the reason this route
	exists at all: every fact on it is one somebody needs where the ground is,
	not where the desk is. "May I walk into block 7" is asked at a gate; "may the
	tractor go on it" is asked from the seat of the tractor.

	OPEN ON ENROLMENT ALONE, exactly as `list_farm_locations` is and for a
	sharper version of the same reason. The restricted-entry layer is the one
	every role sees, always — it is what keeps somebody out of a treated block —
	and gating this read on the dispatch role would have withheld a safety
	warning from the only people it is about. `overlays.layers_for` then narrows
	what comes back to what THIS worker's roles show, and names what it held.

	SCOPED TWICE. `company` narrows on the way in through `guard.require_scope`,
	and the layers themselves are computed off registers `frappe.has_permission`
	has already filtered — the same two-step the Desk page runs, because it is
	the same function.

	`blocks` IS HOW A SCAN BECOMES A MAP ANSWER. A handset that has just scanned
	a block's tag wants that block's five layers and not the farm's, and passing
	one docname is one register read rather than five hundred.
	"""
	allowed = guard.require_scope(user)
	entity = _company(user, company, allowed)

	shown = overlays.layers_for(user)
	wanted, refused = overlays.requested_layers(layers, shown["visible"])

	if isinstance(blocks, str):
		blocks = [part.strip() for part in blocks.replace(",", " ").split() if part.strip()]

	cap = overlays.SUBJECT_CAP
	if limit not in (None, ""):
		try:
			cap = max(1, min(int(limit), overlays.SUBJECT_CAP))
		except (TypeError, ValueError):
			frappe.throw(f"limit must be a whole number, got {limit!r}.", frappe.ValidationError)

	answer = overlays.build(company=entity, visible=wanted, blocks=blocks, limit=cap)
	answer["role"] = shown
	answer["withheld"] = shown["withheld"]
	answer["refused_layers"] = refused
	# `guard.scoped` on the way out as well, on both collections. The registers
	# behind these layers spell the owning entity two ways — `owning_entity` on a
	# Field and `company` on almost everything else — and `overlays.build` reports
	# whichever the register handed it. The outbound check reads `company`, so the
	# rows carry that key, set from the row's own entity.
	answer["blocks"] = guard.scoped(answer.get("blocks") or [], allowed)
	return answer


def _field_boundary_row(row: dict) -> dict:
	"""One Field's row from `list_fields`, trimmed to what draws a shape on a map.

	`list_fields` already carries the polygon — unlike `list_parcels` below, a
	Field's boundary is never stripped from its list answer, because a block IS
	the unit this app's map draws in. This is the same row with the irrigation,
	NDVI and food-safety columns a map overlay does not need left off, so a
	handset loading every block on the farm is not also downloading its spray
	history.
	"""
	return {
		"doctype": farm_tools.FIELD,
		"name": row.get("name"),
		"label": row.get("field_name") or row.get("name"),
		"company": row.get("owning_entity") or None,
		"parcel": row.get("parcel") or None,
		"county": row.get("county") or None,
		"acreage": row.get("acreage"),
		"crop": row.get("crop") or None,
		"variety": row.get("variety") or None,
		"block_number": row.get("block_number") or None,
		"block_ticker": row.get("block_ticker") or None,
		"has_boundary": row.get("has_boundary", False),
		"boundary_geojson": row.get("boundary_geojson"),
		"boundary_centroid": row.get("boundary_centroid"),
		"boundary_bbox_geojson": row.get("boundary_bbox_geojson"),
	}


def _parcel_boundary_fields(names: list) -> dict:
	"""Parcel → `{boundary_geojson, boundary_bbox_geojson}`, for every name given, in one query.

	`list_parcels` deliberately strips the polygon from every row it returns —
	see `_describe_parcel` in `tools/realestate.py` — because a few kilobytes of
	coordinates on every row of a land register nobody asked to draw is weight
	for nothing. This route is the one caller that DOES want to draw them, so it
	reads the two boundary columns back in a single batched query rather than
	calling `get_parcel` once per parcel, which would also run its lease and
	related-asset lookups for a shape it never asked for.
	"""
	wanted = sorted({str(name) for name in (names or []) if name})
	if not wanted:
		return {}
	fields = compat.existing_fields(
		realestate_tools.PARCEL, ("name", "boundary_geojson", "boundary_bbox_geojson")
	)
	rows = frappe.db.get_all(realestate_tools.PARCEL, filters={"name": ("in", wanted)}, fields=fields)
	return {str(row["name"]): dict(row) for row in rows}


def _parcel_boundary_row(row: dict, boundary: dict) -> dict:
	"""One Parcel from `list_parcels`, with its polygon put back for the map."""
	return {
		"doctype": realestate_tools.PARCEL,
		"name": row.get("name"),
		"label": row.get("parcel_name") or row.get("name"),
		"company": row.get("owning_entity") or None,
		"county": row.get("county") or None,
		"state": row.get("state") or None,
		"use_type": row.get("use_type") or None,
		"acreage": row.get("acreage"),
		"has_boundary": row.get("mapped", False),
		"boundary_geojson": boundary.get("boundary_geojson") or None,
		"boundary_centroid": row.get("boundary_centroid"),
		"boundary_bbox_geojson": boundary.get("boundary_bbox_geojson") or None,
	}


# ── 174. list_field_boundaries ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_field_boundaries", limit=guard.READ_LIMIT)
def list_field_boundaries(
	user: str, company=None, include_parcels=None, include_overlays=None, layers=None
) -> dict:
	"""Every block's boundary polygon, for the map view a handset opens first.

	iOS's Today tab draws a `MapPolygon` per block; `get_map_overlays` answers
	what is TRUE of a block and was never meant to carry the shape of one — see
	`_boundary_summary`'s comment on why a list of blocks does not repeat a
	polygon on every reader that has no use for it. This is the reader that does.

	OPEN ON ENROLMENT, for the same reason `list_farm_locations` and
	`get_map_overlays` are: a map with no blocks drawn on it is not a map, and
	every enrolled worker's handset needs one to work from, not only a
	foreman's.

	FIELDS ALWAYS COME BACK WITH THEIR POLYGON, because `list_fields` never
	stripped it. PARCELS ARE OPT IN — `include_parcels` — because a parcel
	usually contains several blocks already drawn, and the boundary is fetched
	for them only when this call is the one place asking for it.

	OVERLAYS RIDE ALONG BY DEFAULT so the same call that draws the shapes can
	also colour them, off the same `overlays.build` `get_map_overlays` calls —
	not a second HTTP round trip through that guarded endpoint, which would
	throttle and audit twice for one screen. `include_overlays=false` skips it
	for a caller that only wants the geometry, and `layers` narrows it exactly
	as it does there.
	"""
	allowed = guard.require_scope(user)
	entity = _company(user, company, allowed)

	inner = {"limit": farm_tools.REGISTER_CAP}
	if entity:
		inner["company"] = entity
	fields = farm_tools.list_fields(inner).data.get("fields") or []
	fields = guard.scoped([{**row, "company": row.get("owning_entity") or None} for row in fields], allowed)
	field_rows = [_field_boundary_row(row) for row in fields]

	parcel_rows = []
	if _as_flag(include_parcels, False):
		try:
			parcels = realestate_tools.list_parcels(inner).data.get("parcels") or []
		except ToolError:
			# `list_parcels` requires a company; an account with none reads as no
			# parcels rather than an error a field-boundary caller cannot act on.
			parcels = []
		parcels = guard.scoped(
			[{**row, "company": row.get("owning_entity") or None} for row in parcels], allowed
		)
		boundaries = _parcel_boundary_fields([row.get("name") for row in parcels])
		parcel_rows = [_parcel_boundary_row(row, boundaries.get(str(row.get("name")), {})) for row in parcels]

	overlay = None
	if _as_flag(include_overlays, True):
		shown = overlays.layers_for(user)
		wanted, refused = overlays.requested_layers(layers, shown["visible"])
		overlay = overlays.build(company=entity, visible=wanted, limit=overlays.SUBJECT_CAP)
		overlay["role"] = shown
		overlay["withheld"] = shown["withheld"]
		overlay["refused_layers"] = refused
		overlay["blocks"] = guard.scoped(overlay.get("blocks") or [], allowed)

	return {
		"company": entity or None,
		"fields": field_rows,
		"field_count": len(field_rows),
		"parcels": parcel_rows,
		"parcel_count": len(parcel_rows),
		"include_parcels": bool(parcel_rows) or _as_flag(include_parcels, False),
		"overlays": overlay,
	}


# ════════════════════════════════════════════════════════════════════════════
# v0.123.0 — THE FOOD-SAFETY, TRACEABILITY, SENSOR AND MARKET REGISTERS
# ════════════════════════════════════════════════════════════════════════════
#
# Seventy-two methods that existed as MCP tools and had no door a handset could
# knock on. The registers behind them are the ones a phone is standing in front
# of when the question comes up: the monitoring log a CCP check is written to,
# the lot code on the bin in front of the picker, the device whose battery just
# died in a block, the residue limit that decides whether a load may ship.
#
# THE `allow_<tool>` SWITCHES DO NOT GATE THIS TRANSPORT. `settings.tool_enabled`
# is read by `mcp.py` and by nothing in `api/guard.py` — those switches are the
# AI surface's gate and they are why a tool is off for the assistant, not why a
# route is closed to a phone. What gates a route here is `guard.endpoint`: the
# mobile kill switch, the rate limit, `FARM_OPS_ROLES` and a live enrolment
# grant, plus whatever the wrapper's own body adds. REGISTERING A ROUTE IS
# PUBLISHING IT to every enrolled handset, so the gate each one carries is
# chosen per method below rather than inherited from a settings page.
#
# READS ARE OPEN ON ENROLMENT, WRITES CARRY `guard.require_dispatch_role`, and
# the competitive registers carry `personnel.require_hr_role()` at BOTH ends —
# a rival's vulnerability windows are a holding-company fact and not a picker's.
# `recall_drill` is gated with the writes despite being read-only: it is a
# management exercise against the lot register, not a read of one's own work.
#
# FIVE METHODS, ACROSS THREE REGISTERS, CANNOT BE SCOPED AND SAY SO. Soil
# Compaction Profile has no `company` column, and the IPM and variety-care
# lookups are keyed on a crop rather than on a document. `require_scoped_doc`
# reads `company` off the row and only refuses when it finds one, so on these it
# reads None, skips its check and hands the docname straight back. They are
# site-wide reference data, named in each docstring rather than left looking
# like an oversight.


# ── 174. get_corrective_action_record ───────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_corrective_action_record", limit=guard.READ_LIMIT)
def get_corrective_action_record(
	user: str,
	corrective_action_record=None,
	record=None,
) -> dict:
	"""One corrective action record in full: the deviation, root cause,
	action taken, product impact, recall determination, and closure
	status.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	allowed = guard.require_scope(user)
	named, _label = _one_spelling(corrective_action_record, record, "corrective_action_record", "record")
	named = guard.require_scoped_doc(
		haccp_tools.CORRECTIVE_ACTION_RECORD, named, "corrective action record", allowed
	)
	inner: dict = {"corrective_action_record": named}
	return haccp_tools.get_corrective_action_record(inner).data


# ── 175. get_food_safety_dashboard ──────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_food_safety_dashboard", limit=guard.READ_LIMIT)
def get_food_safety_dashboard(
	user: str,
	company=None,
) -> dict:
	"""Summary of every food safety plan on the site: QI status, control
	counts, open corrective actions, recall plan currency, and expired.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	return haccp_tools.get_food_safety_dashboard(inner).data


# ── 176. get_food_safety_plan ───────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_food_safety_plan", limit=guard.READ_LIMIT)
def get_food_safety_plan(
	user: str,
	plan=None,
	food_safety_plan=None,
) -> dict:
	"""One food safety plan in full: the QI, lifecycle, covered CTEs, and
	counts of hazard analyses, preventive controls, monitoring records,
	corrective.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	allowed = guard.require_scope(user)
	named, _label = _one_spelling(plan, food_safety_plan, "plan", "food_safety_plan")
	named = guard.require_scoped_doc(haccp_tools.FOOD_SAFETY_PLAN, named, "food safety plan", allowed)
	inner: dict = {"plan": named}
	return haccp_tools.get_food_safety_plan(inner).data


# ── 177. get_hazard_analysis ────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_hazard_analysis", limit=guard.READ_LIMIT)
def get_hazard_analysis(
	user: str,
	hazard_analysis=None,
	hazard=None,
) -> dict:
	"""One hazard analysis record in full: the process step, hazard
	details, risk assessment (likelihood x severity = computed risk
	level), sources,.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	allowed = guard.require_scope(user)
	named, _label = _one_spelling(hazard_analysis, hazard, "hazard_analysis", "hazard")
	named = guard.require_scoped_doc(haccp_tools.HAZARD_ANALYSIS, named, "hazard analysis", allowed)
	inner: dict = {"hazard_analysis": named}
	return haccp_tools.get_hazard_analysis(inner).data


# ── 178. get_monitoring_record ──────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_monitoring_record", limit=guard.READ_LIMIT)
def get_monitoring_record(
	user: str,
	monitoring_record=None,
	record=None,
) -> dict:
	"""One monitoring record in full: the measurement, the control it was
	against, whether it was within the critical limit, and who took it.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	allowed = guard.require_scope(user)
	named, _label = _one_spelling(monitoring_record, record, "monitoring_record", "record")
	named = guard.require_scoped_doc(haccp_tools.MONITORING_RECORD, named, "monitoring record", allowed)
	inner: dict = {"monitoring_record": named}
	return haccp_tools.get_monitoring_record(inner).data


# ── 179. get_preventive_control ─────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_preventive_control", limit=guard.READ_LIMIT)
def get_preventive_control(
	user: str,
	preventive_control=None,
	control=None,
) -> dict:
	"""One preventive control in full: monitoring specs, critical limits,
	corrective action description, verification schedule, and a count
	of.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	allowed = guard.require_scope(user)
	named, _label = _one_spelling(preventive_control, control, "preventive_control", "control")
	named = guard.require_scoped_doc(haccp_tools.PREVENTIVE_CONTROL, named, "preventive control", allowed)
	inner: dict = {"preventive_control": named}
	return haccp_tools.get_preventive_control(inner).data


# ── 180. get_recall_plan ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_recall_plan", limit=guard.READ_LIMIT)
def get_recall_plan(
	user: str,
	recall_plan=None,
	plan_name=None,
) -> dict:
	"""One recall plan in full: coordinators, team contacts, customer
	list,.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	allowed = guard.require_scope(user)
	named, _label = _one_spelling(recall_plan, plan_name, "recall_plan", "plan_name")
	named = guard.require_scoped_doc(haccp_tools.RECALL_PLAN, named, "recall plan", allowed)
	inner: dict = {"recall_plan": named}
	return haccp_tools.get_recall_plan(inner).data


# ── 181. get_supplier_verification ──────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_supplier_verification", limit=guard.READ_LIMIT)
def get_supplier_verification(
	user: str,
	supplier_verification=None,
	verification=None,
) -> dict:
	"""One supplier verification in full: supplier details, product
	supplied, hazards they control, verification method and result, and
	certificate.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	allowed = guard.require_scope(user)
	named, _label = _one_spelling(
		supplier_verification, verification, "supplier_verification", "verification"
	)
	named = guard.require_scoped_doc(
		haccp_tools.SUPPLIER_VERIFICATION, named, "supplier verification", allowed
	)
	inner: dict = {"supplier_verification": named}
	return haccp_tools.get_supplier_verification(inner).data


# ── 182. get_verification_record ────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_verification_record", limit=guard.READ_LIMIT)
def get_verification_record(
	user: str,
	verification_record=None,
	record=None,
) -> dict:
	"""One verification record in full: equipment calibration status,
	result.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	allowed = guard.require_scope(user)
	named, _label = _one_spelling(verification_record, record, "verification_record", "record")
	named = guard.require_scoped_doc(haccp_tools.VERIFICATION_RECORD, named, "verification record", allowed)
	inner: dict = {"verification_record": named}
	return haccp_tools.get_verification_record(inner).data


# ── 183. list_corrective_action_records ─────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_corrective_action_records", limit=guard.READ_LIMIT)
def list_corrective_action_records(
	user: str,
	food_safety_plan=None,
	plan=None,
	preventive_control=None,
	control=None,
	status=None,
	from_date=None,
	to_date=None,
	company=None,
	limit=None,
) -> dict:
	"""Corrective action records — deviations from critical limits and what
	was done about them.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("preventive_control", preventive_control),
		("control", control),
		("status", status),
		("from_date", from_date),
		("to_date", to_date),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.list_corrective_action_records(inner).data


# ── 184. list_food_safety_plans ─────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_food_safety_plans", limit=guard.READ_LIMIT)
def list_food_safety_plans(
	user: str,
	status=None,
	company=None,
	limit=None,
) -> dict:
	"""Every FSMA/HACCP food safety plan on the site, with status, QI info
	and.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("status", status),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.list_food_safety_plans(inner).data


# ── 185. list_hazard_analyses ───────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_hazard_analyses", limit=guard.READ_LIMIT)
def list_hazard_analyses(
	user: str,
	food_safety_plan=None,
	plan=None,
	hazard_type=None,
	risk_level=None,
	company=None,
	limit=None,
) -> dict:
	"""Hazard analyses linked to a food safety plan, optionally filtered by
	hazard type and risk level.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("hazard_type", hazard_type),
		("risk_level", risk_level),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.list_hazard_analyses(inner).data


# ── 186. list_monitoring_records ────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_monitoring_records", limit=guard.READ_LIMIT)
def list_monitoring_records(
	user: str,
	food_safety_plan=None,
	plan=None,
	preventive_control=None,
	control=None,
	from_date=None,
	to_date=None,
	out_of_limit_only=None,
	company=None,
	limit=None,
) -> dict:
	"""Monitoring log entries — actual measurements taken against
	preventive.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("preventive_control", preventive_control),
		("control", control),
		("from_date", from_date),
		("to_date", to_date),
		("out_of_limit_only", out_of_limit_only),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.list_monitoring_records(inner).data


# ── 187. list_preventive_controls ───────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_preventive_controls", limit=guard.READ_LIMIT)
def list_preventive_controls(
	user: str,
	food_safety_plan=None,
	plan=None,
	control_type=None,
	ccp_only=None,
	active_only=None,
	company=None,
	limit=None,
) -> dict:
	"""Preventive controls (and CCPs) for a food safety plan.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("control_type", control_type),
		("ccp_only", ccp_only),
		("active_only", active_only),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.list_preventive_controls(inner).data


# ── 188. list_recall_plans ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_recall_plans", limit=guard.READ_LIMIT)
def list_recall_plans(
	user: str,
	food_safety_plan=None,
	plan=None,
	active_only=None,
	company=None,
	limit=None,
) -> dict:
	"""Recall plans on file — FDA notification procedures, coordinator.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("active_only", active_only),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.list_recall_plans(inner).data


# ── 189. list_supplier_verifications ────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_supplier_verifications", limit=guard.READ_LIMIT)
def list_supplier_verifications(
	user: str,
	food_safety_plan=None,
	plan=None,
	verification_result=None,
	verification_method=None,
	company=None,
	limit=None,
) -> dict:
	"""Supplier verification records — audits, certificate reviews, and
	testing of supply-chain partners.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("verification_result", verification_result),
		("verification_method", verification_method),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.list_supplier_verifications(inner).data


# ── 190. list_verification_records ──────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_verification_records", limit=guard.READ_LIMIT)
def list_verification_records(
	user: str,
	food_safety_plan=None,
	plan=None,
	preventive_control=None,
	control=None,
	verification_type=None,
	from_date=None,
	to_date=None,
	company=None,
	limit=None,
) -> dict:
	"""Verification records — calibration, log review, product testing, and
	sanitation checks.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("preventive_control", preventive_control),
		("control", control),
		("verification_type", verification_type),
		("from_date", from_date),
		("to_date", to_date),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.list_verification_records(inner).data


# ── 191. create_corrective_action_record ────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_corrective_action_record", limit=guard.WRITE_LIMIT, mutating=True)
def create_corrective_action_record(
	user: str,
	food_safety_plan=None,
	plan=None,
	preventive_control=None,
	control=None,
	monitoring_record=None,
	deviation_date=None,
	deviation_description=None,
	root_cause=None,
	action_taken=None,
	action_date=None,
	action_taken_by=None,
	action_taken_by_name=None,
	preventive_measure=None,
	preventive_measure_date=None,
	affected_product_description=None,
	affected_quantity=None,
	affected_quantity_unit=None,
	product_disposition=None,
	recall_determination=None,
	recall_initiated=None,
	company=None,
	notes=None,
) -> dict:
	"""Document a deviation from a critical limit and the corrective action
	taken, including product disposition and recall.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Filing a food-safety record")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("preventive_control", preventive_control),
		("control", control),
		("monitoring_record", monitoring_record),
		("deviation_date", deviation_date),
		("deviation_description", deviation_description),
		("root_cause", root_cause),
		("action_taken", action_taken),
		("action_date", action_date),
		("action_taken_by", action_taken_by),
		("action_taken_by_name", action_taken_by_name),
		("preventive_measure", preventive_measure),
		("preventive_measure_date", preventive_measure_date),
		("affected_product_description", affected_product_description),
		("affected_quantity", affected_quantity),
		("affected_quantity_unit", affected_quantity_unit),
		("product_disposition", product_disposition),
		("recall_determination", recall_determination),
		("recall_initiated", recall_initiated),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.create_corrective_action_record(inner).data


# ── 192. create_food_safety_plan ────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_food_safety_plan", limit=guard.WRITE_LIMIT, mutating=True)
def create_food_safety_plan(
	user: str,
	plan_name=None,
	facility_name=None,
	company=None,
	scope=None,
	status=None,
	covered_activities=None,
	company_gln=None,
	company_address=None,
	qualified_individual=None,
	qualified_individual_name=None,
	qi_certification_expiry=None,
	qi_training_description=None,
	version_number=None,
	effective_date=None,
	review_frequency_months=None,
	last_review_date=None,
	next_review_date=None,
	notes=None,
) -> dict:
	"""Create a new FSMA/HACCP food safety plan — the master document that
	every hazard analysis, preventive control, monitoring.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Filing a food-safety record")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("plan_name", plan_name),
		("facility_name", facility_name),
		("scope", scope),
		("status", status),
		("covered_activities", covered_activities),
		("company_gln", company_gln),
		("company_address", company_address),
		("qualified_individual", qualified_individual),
		("qualified_individual_name", qualified_individual_name),
		("qi_certification_expiry", qi_certification_expiry),
		("qi_training_description", qi_training_description),
		("version_number", version_number),
		("effective_date", effective_date),
		("review_frequency_months", review_frequency_months),
		("last_review_date", last_review_date),
		("next_review_date", next_review_date),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.create_food_safety_plan(inner).data


# ── 193. create_hazard_analysis ─────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_hazard_analysis", limit=guard.WRITE_LIMIT, mutating=True)
def create_hazard_analysis(
	user: str,
	food_safety_plan=None,
	plan=None,
	process_step=None,
	cte_type=None,
	description=None,
	hazard_type=None,
	hazard_name=None,
	hazard_description=None,
	likelihood=None,
	severity=None,
	is_preventable=None,
	potential_sources=None,
	conditions_for_hazard=None,
	company=None,
	notes=None,
) -> dict:
	"""Record a hazard identified at a process step.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Filing a food-safety record")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("process_step", process_step),
		("cte_type", cte_type),
		("description", description),
		("hazard_type", hazard_type),
		("hazard_name", hazard_name),
		("hazard_description", hazard_description),
		("likelihood", likelihood),
		("severity", severity),
		("is_preventable", is_preventable),
		("potential_sources", potential_sources),
		("conditions_for_hazard", conditions_for_hazard),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.create_hazard_analysis(inner).data


# ── 194. create_monitoring_record ───────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_monitoring_record", limit=guard.WRITE_LIMIT, mutating=True)
def create_monitoring_record(
	user: str,
	food_safety_plan=None,
	plan=None,
	preventive_control=None,
	control=None,
	monitoring_date=None,
	monitoring_time=None,
	measured_value=None,
	measured_unit=None,
	observation_notes=None,
	monitored_by=None,
	monitored_by_name=None,
	block=None,
	planting_season=None,
	source_task=None,
	company=None,
	notes=None,
) -> dict:
	"""Log a monitoring measurement against a preventive control.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Filing a food-safety record")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("preventive_control", preventive_control),
		("control", control),
		("monitoring_date", monitoring_date),
		("monitoring_time", monitoring_time),
		("measured_value", measured_value),
		("measured_unit", measured_unit),
		("observation_notes", observation_notes),
		("monitored_by", monitored_by),
		("monitored_by_name", monitored_by_name),
		("block", block),
		("planting_season", planting_season),
		("source_task", source_task),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.create_monitoring_record(inner).data


# ── 195. create_preventive_control ──────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_preventive_control", limit=guard.WRITE_LIMIT, mutating=True)
def create_preventive_control(
	user: str,
	food_safety_plan=None,
	plan=None,
	control_name=None,
	description=None,
	control_type=None,
	is_critical_control_point=None,
	is_active=None,
	monitoring_parameter=None,
	monitoring_frequency=None,
	monitoring_method=None,
	critical_limit=None,
	critical_limit_unit=None,
	critical_limit_operator=None,
	critical_limit_description=None,
	corrective_action_description=None,
	corrective_action_responsible=None,
	verification_frequency=None,
	verification_method=None,
	required_training_description=None,
	company=None,
	notes=None,
) -> dict:
	"""Define a preventive control or CCP with its.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Filing a food-safety record")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("control_name", control_name),
		("description", description),
		("control_type", control_type),
		("is_critical_control_point", is_critical_control_point),
		("is_active", is_active),
		("monitoring_parameter", monitoring_parameter),
		("monitoring_frequency", monitoring_frequency),
		("monitoring_method", monitoring_method),
		("critical_limit", critical_limit),
		("critical_limit_unit", critical_limit_unit),
		("critical_limit_operator", critical_limit_operator),
		("critical_limit_description", critical_limit_description),
		("corrective_action_description", corrective_action_description),
		("corrective_action_responsible", corrective_action_responsible),
		("verification_frequency", verification_frequency),
		("verification_method", verification_method),
		("required_training_description", required_training_description),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.create_preventive_control(inner).data


# ── 196. create_recall_plan ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_recall_plan", limit=guard.WRITE_LIMIT, mutating=True)
def create_recall_plan(
	user: str,
	food_safety_plan=None,
	recall_plan_name=None,
	description=None,
	is_active=None,
	recall_coordinator=None,
	recall_coordinator_name=None,
	recall_coordinator_backup=None,
	recall_coordinator_backup_name=None,
	recall_team_contacts=None,
	customers_list=None,
	product_identification=None,
	fda_notification_required=None,
	fda_notification_procedure=None,
	last_simulation_date=None,
	next_simulation_date=None,
	company=None,
	notes=None,
) -> dict:
	"""Create a recall plan with coordinator contacts, customer list, and
	FDA notification procedures.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Filing a food-safety record")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("food_safety_plan", food_safety_plan),
		("recall_plan_name", recall_plan_name),
		("description", description),
		("is_active", is_active),
		("recall_coordinator", recall_coordinator),
		("recall_coordinator_name", recall_coordinator_name),
		("recall_coordinator_backup", recall_coordinator_backup),
		("recall_coordinator_backup_name", recall_coordinator_backup_name),
		("recall_team_contacts", recall_team_contacts),
		("customers_list", customers_list),
		("product_identification", product_identification),
		("fda_notification_required", fda_notification_required),
		("fda_notification_procedure", fda_notification_procedure),
		("last_simulation_date", last_simulation_date),
		("next_simulation_date", next_simulation_date),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.create_recall_plan(inner).data


# ── 197. create_supplier_verification ───────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_supplier_verification", limit=guard.WRITE_LIMIT, mutating=True)
def create_supplier_verification(
	user: str,
	food_safety_plan=None,
	plan=None,
	supplier_name=None,
	supplier_gln=None,
	supplier_address=None,
	supplier_contact_name=None,
	supplier_contact_phone=None,
	supplier_contact_email=None,
	product_supplied=None,
	product_description=None,
	hazards_controlled_by_supplier=None,
	verification_method=None,
	verification_date=None,
	verification_result=None,
	verification_notes=None,
	certificate_type=None,
	certificate_expiry_date=None,
	next_verification_date=None,
	company=None,
	notes=None,
) -> dict:
	"""Record a supplier verification — an audit,.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Filing a food-safety record")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("supplier_name", supplier_name),
		("supplier_gln", supplier_gln),
		("supplier_address", supplier_address),
		("supplier_contact_name", supplier_contact_name),
		("supplier_contact_phone", supplier_contact_phone),
		("supplier_contact_email", supplier_contact_email),
		("product_supplied", product_supplied),
		("product_description", product_description),
		("hazards_controlled_by_supplier", hazards_controlled_by_supplier),
		("verification_method", verification_method),
		("verification_date", verification_date),
		("verification_result", verification_result),
		("verification_notes", verification_notes),
		("certificate_type", certificate_type),
		("certificate_expiry_date", certificate_expiry_date),
		("next_verification_date", next_verification_date),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.create_supplier_verification(inner).data


# ── 198. create_verification_record ─────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_verification_record", limit=guard.WRITE_LIMIT, mutating=True)
def create_verification_record(
	user: str,
	food_safety_plan=None,
	plan=None,
	preventive_control=None,
	control=None,
	verification_type=None,
	description=None,
	verification_date=None,
	verification_time=None,
	equipment_name=None,
	equipment_calibrated_date=None,
	equipment_calibration_due_date=None,
	calibration_status=None,
	result_summary=None,
	is_control_effective=None,
	findings=None,
	corrective_actions_triggered=None,
	verified_by=None,
	verified_by_name=None,
	company=None,
	notes=None,
) -> dict:
	"""Record a verification activity — calibration, log review, product
	testing, or sanitation check — against a preventive.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Filing a food-safety record")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("preventive_control", preventive_control),
		("control", control),
		("verification_type", verification_type),
		("description", description),
		("verification_date", verification_date),
		("verification_time", verification_time),
		("equipment_name", equipment_name),
		("equipment_calibrated_date", equipment_calibrated_date),
		("equipment_calibration_due_date", equipment_calibration_due_date),
		("calibration_status", calibration_status),
		("result_summary", result_summary),
		("is_control_effective", is_control_effective),
		("findings", findings),
		("corrective_actions_triggered", corrective_actions_triggered),
		("verified_by", verified_by),
		("verified_by_name", verified_by_name),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.create_verification_record(inner).data


# ── 199. update_corrective_action_record ────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("update_corrective_action_record", limit=guard.WRITE_LIMIT, mutating=True)
def update_corrective_action_record(
	user: str,
	corrective_action_record=None,
	record=None,
	status=None,
	deviation_description=None,
	root_cause=None,
	action_taken=None,
	deviation_date=None,
	action_date=None,
	action_taken_by=None,
	action_taken_by_name=None,
	preventive_measure=None,
	preventive_measure_date=None,
	affected_product_description=None,
	affected_quantity=None,
	affected_quantity_unit=None,
	product_disposition=None,
	recall_determination=None,
	recall_initiated=None,
	closed_date=None,
	closure_notes=None,
	company=None,
	notes=None,
) -> dict:
	"""Update a corrective action record — close it,.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Filing a food-safety record")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("corrective_action_record", corrective_action_record),
		("record", record),
		("status", status),
		("deviation_description", deviation_description),
		("root_cause", root_cause),
		("action_taken", action_taken),
		("deviation_date", deviation_date),
		("action_date", action_date),
		("action_taken_by", action_taken_by),
		("action_taken_by_name", action_taken_by_name),
		("preventive_measure", preventive_measure),
		("preventive_measure_date", preventive_measure_date),
		("affected_product_description", affected_product_description),
		("affected_quantity", affected_quantity),
		("affected_quantity_unit", affected_quantity_unit),
		("product_disposition", product_disposition),
		("recall_determination", recall_determination),
		("recall_initiated", recall_initiated),
		("closed_date", closed_date),
		("closure_notes", closure_notes),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.update_corrective_action_record(inner).data


# ── 200. update_food_safety_plan ────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("update_food_safety_plan", limit=guard.WRITE_LIMIT, mutating=True)
def update_food_safety_plan(
	user: str,
	plan=None,
	food_safety_plan=None,
	plan_name=None,
	facility_name=None,
	scope=None,
	status=None,
	covered_activities=None,
	qualified_individual=None,
	qualified_individual_name=None,
	qi_certification_expiry=None,
	qi_training_description=None,
	version_number=None,
	effective_date=None,
	review_frequency_months=None,
	last_review_date=None,
	next_review_date=None,
	company=None,
	notes=None,
) -> dict:
	""".

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Filing a food-safety record")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("plan", plan),
		("food_safety_plan", food_safety_plan),
		("plan_name", plan_name),
		("facility_name", facility_name),
		("scope", scope),
		("status", status),
		("covered_activities", covered_activities),
		("qualified_individual", qualified_individual),
		("qualified_individual_name", qualified_individual_name),
		("qi_certification_expiry", qi_certification_expiry),
		("qi_training_description", qi_training_description),
		("version_number", version_number),
		("effective_date", effective_date),
		("review_frequency_months", review_frequency_months),
		("last_review_date", last_review_date),
		("next_review_date", next_review_date),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.update_food_safety_plan(inner).data


# ── 201. update_hazard_analysis ─────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("update_hazard_analysis", limit=guard.WRITE_LIMIT, mutating=True)
def update_hazard_analysis(
	user: str,
	hazard_analysis=None,
	hazard=None,
	food_safety_plan=None,
	plan=None,
	process_step=None,
	cte_type=None,
	description=None,
	hazard_type=None,
	hazard_name=None,
	hazard_description=None,
	likelihood=None,
	severity=None,
	is_preventable=None,
	potential_sources=None,
	conditions_for_hazard=None,
	company=None,
	notes=None,
) -> dict:
	"""Revise one hazard row on a food safety plan — its likelihood,
	severity, description or preventability.\n\nA HAZARD ANALYSIS IS A
	JUDGEMENT, NOT AN OBSERVATION, which is why this exists where
	`update_monitoring_record` deliberately does not.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Filing a food-safety record")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("hazard_analysis", hazard_analysis),
		("hazard", hazard),
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("process_step", process_step),
		("cte_type", cte_type),
		("description", description),
		("hazard_type", hazard_type),
		("hazard_name", hazard_name),
		("hazard_description", hazard_description),
		("likelihood", likelihood),
		("severity", severity),
		("is_preventable", is_preventable),
		("potential_sources", potential_sources),
		("conditions_for_hazard", conditions_for_hazard),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.update_hazard_analysis(inner).data


# ── 202. update_preventive_control ──────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("update_preventive_control", limit=guard.WRITE_LIMIT, mutating=True)
def update_preventive_control(
	user: str,
	preventive_control=None,
	control=None,
	food_safety_plan=None,
	plan=None,
	control_name=None,
	description=None,
	control_type=None,
	is_critical_control_point=None,
	is_active=None,
	monitoring_parameter=None,
	monitoring_frequency=None,
	monitoring_method=None,
	critical_limit=None,
	critical_limit_unit=None,
	critical_limit_operator=None,
	critical_limit_description=None,
	corrective_action_description=None,
	corrective_action_responsible=None,
	verification_frequency=None,
	verification_method=None,
	required_training_description=None,
	company=None,
	notes=None,
) -> dict:
	"""Update a preventive control's monitoring.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Filing a food-safety record")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("preventive_control", preventive_control),
		("control", control),
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("control_name", control_name),
		("description", description),
		("control_type", control_type),
		("is_critical_control_point", is_critical_control_point),
		("is_active", is_active),
		("monitoring_parameter", monitoring_parameter),
		("monitoring_frequency", monitoring_frequency),
		("monitoring_method", monitoring_method),
		("critical_limit", critical_limit),
		("critical_limit_unit", critical_limit_unit),
		("critical_limit_operator", critical_limit_operator),
		("critical_limit_description", critical_limit_description),
		("corrective_action_description", corrective_action_description),
		("corrective_action_responsible", corrective_action_responsible),
		("verification_frequency", verification_frequency),
		("verification_method", verification_method),
		("required_training_description", required_training_description),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.update_preventive_control(inner).data


# ── 203. update_recall_plan ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("update_recall_plan", limit=guard.WRITE_LIMIT, mutating=True)
def update_recall_plan(
	user: str,
	recall_plan=None,
	plan_name=None,
	food_safety_plan=None,
	recall_plan_name=None,
	description=None,
	is_active=None,
	recall_coordinator=None,
	recall_coordinator_name=None,
	recall_coordinator_backup=None,
	recall_coordinator_backup_name=None,
	recall_team_contacts=None,
	customers_list=None,
	product_identification=None,
	fda_notification_required=None,
	fda_notification_procedure=None,
	last_simulation_date=None,
	next_simulation_date=None,
	company=None,
	notes=None,
) -> dict:
	"""Update a recall plan — change coordinators,.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Filing a food-safety record")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("recall_plan", recall_plan),
		("plan_name", plan_name),
		("food_safety_plan", food_safety_plan),
		("recall_plan_name", recall_plan_name),
		("description", description),
		("is_active", is_active),
		("recall_coordinator", recall_coordinator),
		("recall_coordinator_name", recall_coordinator_name),
		("recall_coordinator_backup", recall_coordinator_backup),
		("recall_coordinator_backup_name", recall_coordinator_backup_name),
		("recall_team_contacts", recall_team_contacts),
		("customers_list", customers_list),
		("product_identification", product_identification),
		("fda_notification_required", fda_notification_required),
		("fda_notification_procedure", fda_notification_procedure),
		("last_simulation_date", last_simulation_date),
		("next_simulation_date", next_simulation_date),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.update_recall_plan(inner).data


# ── 204. update_supplier_verification ───────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("update_supplier_verification", limit=guard.WRITE_LIMIT, mutating=True)
def update_supplier_verification(
	user: str,
	supplier_verification=None,
	verification=None,
	food_safety_plan=None,
	plan=None,
	supplier_name=None,
	supplier_gln=None,
	supplier_address=None,
	supplier_contact_name=None,
	supplier_contact_phone=None,
	supplier_contact_email=None,
	product_supplied=None,
	product_description=None,
	hazards_controlled_by_supplier=None,
	verification_method=None,
	verification_date=None,
	verification_result=None,
	verification_notes=None,
	certificate_type=None,
	certificate_expiry_date=None,
	next_verification_date=None,
	company=None,
	notes=None,
) -> dict:
	"""Update a supplier verification — new audit.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Filing a food-safety record")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("supplier_verification", supplier_verification),
		("verification", verification),
		("food_safety_plan", food_safety_plan),
		("plan", plan),
		("supplier_name", supplier_name),
		("supplier_gln", supplier_gln),
		("supplier_address", supplier_address),
		("supplier_contact_name", supplier_contact_name),
		("supplier_contact_phone", supplier_contact_phone),
		("supplier_contact_email", supplier_contact_email),
		("product_supplied", product_supplied),
		("product_description", product_description),
		("hazards_controlled_by_supplier", hazards_controlled_by_supplier),
		("verification_method", verification_method),
		("verification_date", verification_date),
		("verification_result", verification_result),
		("verification_notes", verification_notes),
		("certificate_type", certificate_type),
		("certificate_expiry_date", certificate_expiry_date),
		("next_verification_date", next_verification_date),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return haccp_tools.update_supplier_verification(inner).data


# ── 205. get_lot_timeline ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_lot_timeline", limit=guard.READ_LIMIT)
def get_lot_timeline(
	user: str,
	lot_code=None,
	limit=None,
) -> dict:
	"""ONE LOT'S EVENTS IN ORDER, WITH THE REFERENCED RECORDS RESOLVED —
	the document an audit asks for.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(lot_tools.DOCTYPE, lot_code, "lot code", allowed)
	inner: dict = {"lot_code": named}
	for key, given in (("limit", limit),):
		if given not in (None, ""):
			inner[key] = given
	return lot_tools.get_lot_timeline(inner).data


# ── 206. get_traceability_lot ───────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_traceability_lot", limit=guard.READ_LIMIT)
def get_traceability_lot(
	user: str,
	lot_code=None,
) -> dict:
	"""One lot with every Critical Tracking Event filed against it, its
	source lots and its status.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(lot_tools.DOCTYPE, lot_code, "lot code", allowed)
	inner: dict = {"lot_code": named}
	return lot_tools.get_traceability_lot(inner).data


# ── 207. list_traceability_lots ─────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_traceability_lots", limit=guard.READ_LIMIT)
def list_traceability_lots(
	user: str,
	field=None,
	variety=None,
	date_from=None,
	date_to=None,
	status=None,
	harvest_shift=None,
	planting_season=None,
	company=None,
	limit=None,
) -> dict:
	"""The lot register — by block, variety, day, status, shift or planting
	season.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("field", field),
		("variety", variety),
		("date_from", date_from),
		("date_to", date_to),
		("status", status),
		("harvest_shift", harvest_shift),
		("planting_season", planting_season),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return lot_tools.list_traceability_lots(inner).data


# ── 208. recall_drill ───────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("recall_drill", limit=guard.READ_LIMIT)
def recall_drill(
	user: str,
	lot_code=None,
) -> dict:
	"""THE TWENTY-FOUR-HOUR ANSWER: everywhere this lot went, who received
	it and when.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	guard.require_dispatch_role(user, "Running a recall drill")
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(lot_tools.DOCTYPE, lot_code, "lot code", allowed)
	inner: dict = {"lot_code": named}
	return lot_tools.recall_drill(inner).data


# ── 209. trace_lot_backward ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("trace_lot_backward", limit=guard.READ_LIMIT)
def trace_lot_backward(
	user: str,
	lot_code=None,
) -> dict:
	"""EVERYTHING UPSTREAM OF ONE LOT: its source lots, their source lots,
	the blocks at the roots of that chain, and then THE SPRAY REGISTER —
	bounded at each root lot's own harvest date.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(lot_tools.DOCTYPE, lot_code, "lot code", allowed)
	inner: dict = {"lot_code": named}
	return lot_tools.trace_lot_backward(inner).data


# ── 210. trace_lot_forward ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("trace_lot_forward", limit=guard.READ_LIMIT)
def trace_lot_forward(
	user: str,
	lot_code=None,
) -> dict:
	"""WHICH LOTS CARRY THIS LOT'S FRUIT, AND WHO HAS THEM.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(lot_tools.DOCTYPE, lot_code, "lot code", allowed)
	inner: dict = {"lot_code": named}
	return lot_tools.trace_lot_forward(inner).data


# ── 211. create_traceability_lot ────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_traceability_lot", limit=guard.WRITE_LIMIT, mutating=True)
def create_traceability_lot(
	user: str,
	field=None,
	variety=None,
	harvest_date=None,
	company=None,
	lot_code=None,
	status=None,
	harvest_shift=None,
	shift=None,
	planting_season=None,
	quantity=None,
	quantity_uom=None,
	source_lots=None,
	location=None,
	actor=None,
	allow_duplicate=None,
	notes=None,
) -> dict:
	"""ASSIGN THE ONE IDENTIFIER FSMA 204 ACTUALLY REQUIRES to one day's
	fruit off one block.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Writing to the lot register")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("field", field),
		("variety", variety),
		("harvest_date", harvest_date),
		("lot_code", lot_code),
		("status", status),
		("harvest_shift", harvest_shift),
		("shift", shift),
		("planting_season", planting_season),
		("quantity", quantity),
		("quantity_uom", quantity_uom),
		("source_lots", source_lots),
		("location", location),
		("actor", actor),
		("allow_duplicate", allow_duplicate),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return lot_tools.create_traceability_lot(inner).data


# ── 212. index_lot_events ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("index_lot_events", limit=guard.WRITE_LIMIT, mutating=True)
def index_lot_events(
	user: str,
	date_from=None,
	date_to=None,
	company=None,
) -> dict:
	"""MUTATING (default OFF), idempotent.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Writing to the lot register")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("date_from", date_from),
		("date_to", date_to),
	):
		if given not in (None, ""):
			inner[key] = given
	return lot_tools.index_lot_events(inner).data


# ── 213. record_cte ─────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("record_cte", limit=guard.WRITE_LIMIT, mutating=True)
def record_cte(
	user: str,
	lot_code=None,
	event_type=None,
	reference_doctype=None,
	reference_name=None,
	actor=None,
	location=None,
	description=None,
	quantity=None,
	quantity_uom=None,
	source_location=None,
	destination_location=None,
	carrier=None,
	receiver=None,
	event_datetime=None,
	when=None,
	company=None,
) -> dict:
	"""FILE ONE CRITICAL TRACKING EVENT against one lot — the FSMA 204 unit
	of record.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Writing to the lot register")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("lot_code", lot_code),
		("event_type", event_type),
		("reference_doctype", reference_doctype),
		("reference_name", reference_name),
		("actor", actor),
		("location", location),
		("description", description),
		("quantity", quantity),
		("quantity_uom", quantity_uom),
		("source_location", source_location),
		("destination_location", destination_location),
		("carrier", carrier),
		("receiver", receiver),
		("event_datetime", event_datetime),
		("when", when),
	):
		if given not in (None, ""):
			inner[key] = given
	return lot_tools.record_cte(inner).data


# ── 214. trace_backward ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("trace_backward", limit=guard.READ_LIMIT)
def trace_backward(
	user: str,
	shipment=None,
	bin=None,
	scale_ticket=None,
	settlement=None,
	bucket_entry=None,
	company=None,
	date_from=None,
	date_to=None,
	limit=None,
) -> dict:
	"""THE MOCK RECALL, BACKWARDS: everything that happened to one lot.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("shipment", shipment),
		("bin", bin),
		("scale_ticket", scale_ticket),
		("settlement", settlement),
		("bucket_entry", bucket_entry),
		("date_from", date_from),
		("date_to", date_to),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return trace_tools.trace_backward(inner).data


# ── 215. trace_forward ──────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("trace_forward", limit=guard.READ_LIMIT)
def trace_forward(
	user: str,
	block=None,
	spray_application=None,
	water_test=None,
	company=None,
	date_from=None,
	date_to=None,
	limit=None,
) -> dict:
	"""THE MOCK RECALL, FORWARDS: which lots carry product from here, and
	WHO HAS THEM.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("block", block),
		("spray_application", spray_application),
		("water_test", water_test),
		("date_from", date_from),
		("date_to", date_to),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return trace_tools.trace_forward(inner).data


# ── 216. get_device_readings ────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_device_readings", limit=guard.READ_LIMIT)
def get_device_readings(
	user: str,
	device=None,
	reading_type=None,
	quality=None,
	from_timestamp=None,
	to_timestamp=None,
	from_date=None,
	to_date=None,
) -> dict:
	"""One device's readings over a date range, summarised per reading
	type: count, min, max, mean, latest, and the window they actually
	span.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(iot_tools.DEVICE, device, "device", allowed)
	inner: dict = {"device": named}
	for key, given in (
		("reading_type", reading_type),
		("quality", quality),
		("from_timestamp", from_timestamp),
		("to_timestamp", to_timestamp),
		("from_date", from_date),
		("to_date", to_date),
	):
		if given not in (None, ""):
			inner[key] = given
	return iot_tools.get_device_readings(inner).data


# ── 217. get_iot_device ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_iot_device", limit=guard.READ_LIMIT)
def get_iot_device(
	user: str,
	device=None,
) -> dict:
	"""One device in full, with its health picture and its most recent
	reading.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(iot_tools.DEVICE, device, "device", allowed)
	inner: dict = {"device": named}
	return iot_tools.get_iot_device(inner).data


# ── 218. list_iot_devices ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_iot_devices", limit=guard.READ_LIMIT)
def list_iot_devices(
	user: str,
	company=None,
	field=None,
	device_type=None,
	device_class=None,
	enabled=None,
	online=None,
	limit=None,
) -> dict:
	"""The device register, counted by type, with the offline, never-
	reported and low-battery ones NAMED rather than merely counted —
	'three are offline' is not actionable and 'the two probes in Yellow
	Camp are offline' is.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("field", field),
		("device_type", device_type),
		("device_class", device_class),
		("enabled", enabled),
		("online", online),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return iot_tools.list_iot_devices(inner).data


# ── 219. list_iot_readings ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_iot_readings", limit=guard.READ_LIMIT)
def list_iot_readings(
	user: str,
	device=None,
	field=None,
	company=None,
	reading_type=None,
	quality=None,
	from_timestamp=None,
	to_timestamp=None,
	from_date=None,
	to_date=None,
	limit=None,
) -> dict:
	"""Readings matching the filters, newest first — ROWS, not statistics.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("device", device),
		("field", field),
		("reading_type", reading_type),
		("quality", quality),
		("from_timestamp", from_timestamp),
		("to_timestamp", to_timestamp),
		("from_date", from_date),
		("to_date", to_date),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return iot_tools.list_iot_readings(inner).data


# ── 220. create_iot_device ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_iot_device", limit=guard.WRITE_LIMIT, mutating=True)
def create_iot_device(
	user: str,
	company=None,
	device_name=None,
	hardware_id=None,
	device_type=None,
	device_class=None,
	field=None,
	zone=None,
	enabled=None,
	calibrated_on=None,
	device_config=None,
	notes=None,
) -> dict:
	"""Register one field device — a soil probe, a flow meter, a weather
	station — and mint the bearer token it will post with.\n\nTHE TOKEN
	IS SHOWN ONCE AND NEVER READ BACK.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Writing to the device register")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("device_name", device_name),
		("hardware_id", hardware_id),
		("device_type", device_type),
		("device_class", device_class),
		("field", field),
		("zone", zone),
		("enabled", enabled),
		("calibrated_on", calibrated_on),
		("device_config", device_config),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return iot_tools.create_iot_device(inner).data


# ── 221. create_iot_reading ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_iot_reading", limit=guard.WRITE_LIMIT, mutating=True)
def create_iot_reading(
	user: str,
	device=None,
	timestamp=None,
	reading_type=None,
	value=None,
	unit=None,
	quality=None,
	notes=None,
) -> dict:
	"""Record one measurement from one device, and mark that device as
	having spoken.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	guard.require_dispatch_role(user, "Writing to the device register")
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(iot_tools.DEVICE, device, "device", allowed)
	inner: dict = {"device": named}
	for key, given in (
		("timestamp", timestamp),
		("reading_type", reading_type),
		("value", value),
		("unit", unit),
		("quality", quality),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return iot_tools.create_iot_reading(inner).data


# ── 222. update_iot_device ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("update_iot_device", limit=guard.WRITE_LIMIT, mutating=True)
def update_iot_device(
	user: str,
	device=None,
	device_name=None,
	hardware_id=None,
	device_type=None,
	device_class=None,
	field=None,
	zone=None,
	enabled=None,
	battery_level=None,
	signal_strength=None,
	calibrated_on=None,
	device_config=None,
	notes=None,
) -> dict:
	"""Change a device's placement, health figures, calibration date or
	config.\n\n`last_seen` IS REFUSED HERE AND THAT IS THE POINT OF THE
	COLUMN.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	guard.require_dispatch_role(user, "Writing to the device register")
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(iot_tools.DEVICE, device, "device", allowed)
	inner: dict = {"device": named}
	for key, given in (
		("device_name", device_name),
		("hardware_id", hardware_id),
		("device_type", device_type),
		("device_class", device_class),
		("field", field),
		("zone", zone),
		("enabled", enabled),
		("battery_level", battery_level),
		("signal_strength", signal_strength),
		("calibrated_on", calibrated_on),
		("device_config", device_config),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return iot_tools.update_iot_device(inner).data


# ── 223. get_ipm_reference ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_ipm_reference", limit=guard.READ_LIMIT)
def get_ipm_reference(
	user: str,
	pest=None,
	product=None,
	beneficial=None,
	crop=None,
	table=None,
) -> dict:
	"""The published IPM reference book this app carries: degree-day
	emergence models, pest damage windows, beneficial organisms and when
	they are active, product efficacy with IRAC and FRAC groups,
	product-level pre-harvest and re-entry intervals, and — the half
	almost no label carries — what each product does to each beneficial.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	THIS REGISTER IS SITE-WIDE AND CARRIES NO `company` COLUMN, so there
	is nothing to scope it to and `guard.require_scoped_doc` would read
	None and check nothing. It is reference data — the same rows for
	every entity on the bench — and it is named here rather than left to
	look like an oversight.
	"""
	# Enrolment and scope are still PROVEN here even though nothing below
	# consumes the list: `require_scope` raises for a caller with no
	# company at all, and this register has none to filter on.
	guard.require_scope(user)
	inner: dict = {}
	for key, given in (
		("pest", pest),
		("product", product),
		("beneficial", beneficial),
		("crop", crop),
		("table", table),
	):
		if given not in (None, ""):
			inner[key] = given
	return mrl_tools.get_ipm_reference(inner).data


# ── 224. get_mrl_for_chemical_crop_market ───────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_mrl_for_chemical_crop_market", limit=guard.READ_LIMIT)
def get_mrl_for_chemical_crop_market(
	user: str,
	chemical=None,
	crop=None,
	market=None,
	company=None,
) -> dict:
	"""The limit for one lane — this ingredient, this fruit, this
	destination.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("chemical", chemical),
		("crop", crop),
		("market", market),
	):
		if given not in (None, ""):
			inner[key] = given
	return mrl_tools.get_mrl_for_chemical_crop_market(inner).data


# ── 225. get_mrl_record ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_mrl_record", limit=guard.READ_LIMIT)
def get_mrl_record(
	user: str,
	mrl_record=None,
) -> dict:
	"""One limit in full, with everything that qualifies it — the source
	tier, whether it is a blanket default, whether it was matched
	through a crop group, and whether it is past its re-check date.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(mrl_tools.MRL, mrl_record, "MRL record", allowed)
	inner: dict = {"mrl_record": named}
	return mrl_tools.get_mrl_record(inner).data


# ── 226. list_mrl_records ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_mrl_records", limit=guard.READ_LIMIT)
def list_mrl_records(
	user: str,
	chemical=None,
	crop=None,
	market=None,
	company=None,
	source_tier=None,
	confidence=None,
	substance_status=None,
	needs_recheck=None,
	limit=None,
) -> dict:
	"""Limits on file, counted by market, with the stale, inferred, default
	and banned ones each named.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("chemical", chemical),
		("crop", crop),
		("market", market),
		("source_tier", source_tier),
		("confidence", confidence),
		("substance_status", substance_status),
		("needs_recheck", needs_recheck),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return mrl_tools.list_mrl_records(inner).data


# ── 227. create_mrl_record ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_mrl_record", limit=guard.WRITE_LIMIT, mutating=True)
def create_mrl_record(
	user: str,
	chemical=None,
	crop=None,
	market=None,
	mrl_ppm=None,
	source=None,
	company=None,
	source_tier=None,
	confidence=None,
	substance_status=None,
	is_default_mrl=None,
	crop_group_match=None,
	effective_date=None,
	expiry_date=None,
	research_notes=None,
	research_response=None,
	notes=None,
) -> dict:
	"""Record the maximum residue limit for one active ingredient on one
	crop into one destination market.\n\nTHE SOURCE IS REQUIRED AND ITS
	ABSENCE IS THE FAILURE THIS PREVENTS.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Writing to the residue-limit register")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("chemical", chemical),
		("crop", crop),
		("market", market),
		("mrl_ppm", mrl_ppm),
		("source", source),
		("source_tier", source_tier),
		("confidence", confidence),
		("substance_status", substance_status),
		("is_default_mrl", is_default_mrl),
		("crop_group_match", crop_group_match),
		("effective_date", effective_date),
		("expiry_date", expiry_date),
		("research_notes", research_notes),
		("research_response", research_response),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return mrl_tools.create_mrl_record(inner).data


# ── 228. update_mrl_record ──────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("update_mrl_record", limit=guard.WRITE_LIMIT, mutating=True)
def update_mrl_record(
	user: str,
	mrl_record=None,
	chemical=None,
	crop=None,
	market=None,
	mrl_ppm=None,
	source=None,
	source_tier=None,
	confidence=None,
	substance_status=None,
	is_default_mrl=None,
	crop_group_match=None,
	effective_date=None,
	expiry_date=None,
	research_notes=None,
	research_response=None,
	notes=None,
) -> dict:
	"""Revise a limit — most often because the regulator did.\n\nMOVING THE
	NUMBER WITHOUT MOVING THE SOURCE IS REPORTED BACK.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	guard.require_dispatch_role(user, "Writing to the residue-limit register")
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(mrl_tools.MRL, mrl_record, "MRL record", allowed)
	inner: dict = {"mrl_record": named}
	for key, given in (
		("chemical", chemical),
		("crop", crop),
		("market", market),
		("mrl_ppm", mrl_ppm),
		("source", source),
		("source_tier", source_tier),
		("confidence", confidence),
		("substance_status", substance_status),
		("is_default_mrl", is_default_mrl),
		("crop_group_match", crop_group_match),
		("effective_date", effective_date),
		("expiry_date", expiry_date),
		("research_notes", research_notes),
		("research_response", research_response),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return mrl_tools.update_mrl_record(inner).data


# ── 229. list_soil_compaction_profiles ──────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_soil_compaction_profiles", limit=guard.READ_LIMIT)
def list_soil_compaction_profiles(
	user: str,
	enabled_only=None,
	limit=None,
) -> dict:
	"""The soil book behind the compaction colours: for each soil, how many
	hours after the water comes off the ground is still red, and how
	many until it is green.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	THIS REGISTER IS SITE-WIDE AND CARRIES NO `company` COLUMN, so there
	is nothing to scope it to and `guard.require_scoped_doc` would read
	None and check nothing. It is reference data — the same rows for
	every entity on the bench — and it is named here rather than left to
	look like an oversight.
	"""
	# Enrolment and scope are still PROVEN here even though nothing below
	# consumes the list: `require_scope` raises for a caller with no
	# company at all, and this register has none to filter on.
	guard.require_scope(user)
	inner: dict = {}
	for key, given in (
		("enabled_only", enabled_only),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return map_overlay_tools.list_soil_compaction_profiles(inner).data


# ── 230. assign_soil_profile ────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("assign_soil_profile", limit=guard.WRITE_LIMIT, mutating=True)
def assign_soil_profile(
	user: str,
	field=None,
	soil_profile=None,
	clear=None,
	company=None,
	dry_run=None,
) -> dict:
	"""MUTATING (default OFF), idempotent.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	guard.require_dispatch_role(user, "Editing the soil-compaction reference")
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("field", field),
		("soil_profile", soil_profile),
		("clear", clear),
		("dry_run", dry_run),
	):
		if given not in (None, ""):
			inner[key] = given
	return map_overlay_tools.assign_soil_profile(inner).data


# ── 231. create_soil_compaction_profile ─────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_soil_compaction_profile", limit=guard.WRITE_LIMIT, mutating=True)
def create_soil_compaction_profile(
	user: str,
	soil_type=None,
	red_hours=None,
	yellow_hours=None,
	drainage_class=None,
	source=None,
	notes=None,
	enabled=None,
) -> dict:
	"""Add a soil this farm has that the shipped book does not.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	THIS REGISTER IS SITE-WIDE AND CARRIES NO `company` COLUMN, so there
	is nothing to scope it to and `guard.require_scoped_doc` would read
	None and check nothing. It is reference data — the same rows for
	every entity on the bench — and it is named here rather than left to
	look like an oversight.
	"""
	guard.require_dispatch_role(user, "Editing the soil-compaction reference")
	# Enrolment and scope are still PROVEN here even though nothing below
	# consumes the list: `require_scope` raises for a caller with no
	# company at all, and this register has none to filter on.
	guard.require_scope(user)
	inner: dict = {}
	for key, given in (
		("soil_type", soil_type),
		("red_hours", red_hours),
		("yellow_hours", yellow_hours),
		("drainage_class", drainage_class),
		("source", source),
		("notes", notes),
		("enabled", enabled),
	):
		if given not in (None, ""):
			inner[key] = given
	return map_overlay_tools.create_soil_compaction_profile(inner).data


# ── 232. update_soil_compaction_profile ─────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("update_soil_compaction_profile", limit=guard.WRITE_LIMIT, mutating=True)
def update_soil_compaction_profile(
	user: str,
	soil_type=None,
	red_hours=None,
	yellow_hours=None,
	drainage_class=None,
	source=None,
	notes=None,
	enabled=None,
) -> dict:
	"""MUTATING (default OFF), idempotent.

	THE DISPATCH GATE. Every read beside this one is open on enrolment;
	writing is what a foreman does, and `guard.require_dispatch_role` is
	the frozenset that says so.

	THIS REGISTER IS SITE-WIDE AND CARRIES NO `company` COLUMN, so there
	is nothing to scope it to and `guard.require_scoped_doc` would read
	None and check nothing. It is reference data — the same rows for
	every entity on the bench — and it is named here rather than left to
	look like an oversight.
	"""
	guard.require_dispatch_role(user, "Editing the soil-compaction reference")
	# Enrolment and scope are still PROVEN here even though nothing below
	# consumes the list: `require_scope` raises for a caller with no
	# company at all, and this register has none to filter on.
	guard.require_scope(user)
	inner: dict = {}
	for key, given in (
		("soil_type", soil_type),
		("red_hours", red_hours),
		("yellow_hours", yellow_hours),
		("drainage_class", drainage_class),
		("source", source),
		("notes", notes),
		("enabled", enabled),
	):
		if given not in (None, ""):
			inner[key] = given
	return map_overlay_tools.update_soil_compaction_profile(inner).data


# ── 233. get_variety_care_recipe ────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_variety_care_recipe", limit=guard.READ_LIMIT)
def get_variety_care_recipe(
	user: str,
	crop=None,
	variety=None,
) -> dict:
	"""One variety's care recipe, RESOLVED: its water schedule and its
	cultural practice protocol — GA timings, PGR program, thinning
	approach, pruning.

	OPEN ON ENROLMENT. `guard.endpoint` has already required a Farm Ops
	role and a live mobile grant; this read adds nothing beyond the
	company scope its own answer is filtered to.

	THIS REGISTER IS SITE-WIDE AND CARRIES NO `company` COLUMN, so there
	is nothing to scope it to and `guard.require_scoped_doc` would read
	None and check nothing. It is reference data — the same rows for
	every entity on the bench — and it is named here rather than left to
	look like an oversight.
	"""
	# Enrolment and scope are still PROVEN here even though nothing below
	# consumes the list: `require_scope` raises for a caller with no
	# company at all, and this register has none to filter on.
	guard.require_scope(user)
	inner: dict = {}
	for key, given in (
		("crop", crop),
		("variety", variety),
	):
		if given not in (None, ""):
			inner[key] = given
	return agronomy_tools.get_variety_care_recipe(inner).data


# ── 234. get_acquisition_target ─────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_acquisition_target", limit=guard.READ_LIMIT)
def get_acquisition_target(
	user: str,
	acquisition_target=None,
) -> dict:
	"""One target in full: the four fit scores with the weakest named, the
	asset breakdown totalled against the going-concern estimate, and the
	participant record behind it.

	THE HR GATE, not the field-ops one. A competitive register names
	other businesses and what this one thinks of them; it is a holding-
	company fact, and the picker holding the phone is entitled to their
	own work rather than to a rival's vulnerability windows.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(
		compintel_tools.TARGET, acquisition_target, "acquisition_target", allowed
	)
	inner: dict = {"acquisition_target": named}
	return compintel_tools.get_acquisition_target(inner).data


# ── 235. get_competitive_move ───────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_competitive_move", limit=guard.READ_LIMIT)
def get_competitive_move(
	user: str,
	competitive_move=None,
) -> dict:
	""".

	THE HR GATE, not the field-ops one. A competitive register names
	other businesses and what this one thinks of them; it is a holding-
	company fact, and the picker holding the phone is entitled to their
	own work rather than to a rival's vulnerability windows.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(compintel_tools.MOVE, competitive_move, "competitive_move", allowed)
	inner: dict = {"competitive_move": named}
	return compintel_tools.get_competitive_move(inner).data


# ── 236. get_market_participant ─────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_market_participant", limit=guard.READ_LIMIT)
def get_market_participant(
	user: str,
	market_participant=None,
) -> dict:
	"""One participant in full, with every move observed against them and
	any acquisition target opened on them.

	THE HR GATE, not the field-ops one. A competitive register names
	other businesses and what this one thinks of them; it is a holding-
	company fact, and the picker holding the phone is entitled to their
	own work rather than to a rival's vulnerability windows.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(
		compintel_tools.PARTICIPANT, market_participant, "market_participant", allowed
	)
	inner: dict = {"market_participant": named}
	return compintel_tools.get_market_participant(inner).data


# ── 237. list_acquisition_targets ───────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_acquisition_targets", limit=guard.READ_LIMIT)
def list_acquisition_targets(
	user: str,
	company=None,
	status=None,
	action_level=None,
	market_participant=None,
	strategic_plan=None,
	limit=None,
) -> dict:
	"""The pipeline, best-scored first, counted by status.

	THE HR GATE, not the field-ops one. A competitive register names
	other businesses and what this one thinks of them; it is a holding-
	company fact, and the picker holding the phone is entitled to their
	own work rather than to a rival's vulnerability windows.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("status", status),
		("action_level", action_level),
		("market_participant", market_participant),
		("strategic_plan", strategic_plan),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return compintel_tools.list_acquisition_targets(inner).data


# ── 238. list_competitive_moves ─────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_competitive_moves", limit=guard.READ_LIMIT)
def list_competitive_moves(
	user: str,
	company=None,
	market_participant=None,
	move_type=None,
	severity=None,
	confidence=None,
	response_urgency=None,
	strategic_plan=None,
	unanswered=None,
	from_date=None,
	to_date=None,
	limit=None,
) -> dict:
	"""Moves observed, newest first, counted by type and by participant,
	with the unanswered ones named.

	THE HR GATE, not the field-ops one. A competitive register names
	other businesses and what this one thinks of them; it is a holding-
	company fact, and the picker holding the phone is entitled to their
	own work rather than to a rival's vulnerability windows.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("market_participant", market_participant),
		("move_type", move_type),
		("severity", severity),
		("confidence", confidence),
		("response_urgency", response_urgency),
		("strategic_plan", strategic_plan),
		("unanswered", unanswered),
		("from_date", from_date),
		("to_date", to_date),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return compintel_tools.list_competitive_moves(inner).data


# ── 239. list_market_participants ───────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_market_participants", limit=guard.READ_LIMIT)
def list_market_participants(
	user: str,
	company=None,
	participant_type=None,
	market_position=None,
	relationship_status=None,
	geography=None,
	industry_segment=None,
	strategic_plan=None,
	limit=None,
) -> dict:
	"""The participant register, counted by type, with the ones nobody has
	assessed named.

	THE HR GATE, not the field-ops one. A competitive register names
	other businesses and what this one thinks of them; it is a holding-
	company fact, and the picker holding the phone is entitled to their
	own work rather than to a rival's vulnerability windows.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("participant_type", participant_type),
		("market_position", market_position),
		("relationship_status", relationship_status),
		("geography", geography),
		("industry_segment", industry_segment),
		("strategic_plan", strategic_plan),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return compintel_tools.list_market_participants(inner).data


# ── 240. create_acquisition_target ──────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_acquisition_target", limit=guard.WRITE_LIMIT, mutating=True)
def create_acquisition_target(
	user: str,
	company=None,
	entity_name=None,
	market_participant=None,
	strategic_plan=None,
	status=None,
	action_level=None,
	strategic_fit_score=None,
	financial_health_score=None,
	synergy_score=None,
	cultural_fit_score=None,
	estimated_value=None,
	estimated_acquisition_cost=None,
	projected_revenue_uplift=None,
	projected_cost_savings=None,
	acreage=None,
	payback_period_years=None,
	irr_estimate=None,
	intergenerational_horizon_years=None,
	land_value_appreciation=None,
	water_rights_value=None,
	varietal_ip_value=None,
	infrastructure_value=None,
	identified_date=None,
	target_close_date=None,
	actual_close_date=None,
	rationale=None,
	recommendation=None,
	notes=None,
) -> dict:
	"""Open a file on a farm somebody is considering buying, scored on the
	four dimensions that decide whether it works.\n\nTHE FOUR SCORES ARE
	SEPARATE BECAUSE THEY FAIL SEPARATELY.

	THE HR GATE, not the field-ops one. A competitive register names
	other businesses and what this one thinks of them; it is a holding-
	company fact, and the picker holding the phone is entitled to their
	own work rather than to a rival's vulnerability windows.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("entity_name", entity_name),
		("market_participant", market_participant),
		("strategic_plan", strategic_plan),
		("status", status),
		("action_level", action_level),
		("strategic_fit_score", strategic_fit_score),
		("financial_health_score", financial_health_score),
		("synergy_score", synergy_score),
		("cultural_fit_score", cultural_fit_score),
		("estimated_value", estimated_value),
		("estimated_acquisition_cost", estimated_acquisition_cost),
		("projected_revenue_uplift", projected_revenue_uplift),
		("projected_cost_savings", projected_cost_savings),
		("acreage", acreage),
		("payback_period_years", payback_period_years),
		("irr_estimate", irr_estimate),
		("intergenerational_horizon_years", intergenerational_horizon_years),
		("land_value_appreciation", land_value_appreciation),
		("water_rights_value", water_rights_value),
		("varietal_ip_value", varietal_ip_value),
		("infrastructure_value", infrastructure_value),
		("identified_date", identified_date),
		("target_close_date", target_close_date),
		("actual_close_date", actual_close_date),
		("rationale", rationale),
		("recommendation", recommendation),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return compintel_tools.create_acquisition_target(inner).data


# ── 241. create_competitive_move ────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_competitive_move", limit=guard.WRITE_LIMIT, mutating=True)
def create_competitive_move(
	user: str,
	company=None,
	market_participant=None,
	move_type=None,
	description=None,
	observed_date=None,
	strategic_plan=None,
	severity=None,
	source=None,
	confidence=None,
	impact_assessment=None,
	market_impact_pct=None,
	revenue_impact=None,
	response_urgency=None,
	recommended_response=None,
	notes=None,
) -> dict:
	"""Record something a competitor actually did, on the day somebody
	noticed it.\n\nMOVES ONLY MEAN ANYTHING IN SEQUENCE.

	THE HR GATE, not the field-ops one. A competitive register names
	other businesses and what this one thinks of them; it is a holding-
	company fact, and the picker holding the phone is entitled to their
	own work rather than to a rival's vulnerability windows.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("market_participant", market_participant),
		("move_type", move_type),
		("description", description),
		("observed_date", observed_date),
		("strategic_plan", strategic_plan),
		("severity", severity),
		("source", source),
		("confidence", confidence),
		("impact_assessment", impact_assessment),
		("market_impact_pct", market_impact_pct),
		("revenue_impact", revenue_impact),
		("response_urgency", response_urgency),
		("recommended_response", recommended_response),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return compintel_tools.create_competitive_move(inner).data


# ── 242. create_market_participant ──────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_market_participant", limit=guard.WRITE_LIMIT, mutating=True)
def create_market_participant(
	user: str,
	company=None,
	participant_name=None,
	participant_type=None,
	customer=None,
	strategic_plan=None,
	relationship_status=None,
	industry_segment=None,
	geography=None,
	crops=None,
	market_position=None,
	market_share_pct=None,
	employee_count=None,
	estimated_revenue=None,
	estimated_acreage=None,
	strengths=None,
	weaknesses=None,
	key_assets=None,
	vulnerability_windows=None,
	notes=None,
) -> dict:
	"""Open a record for another organisation in this market — a
	competitor, supplier, buyer, partner or acquisition target.\n\nTHE
	TYPE IS A COLUMN RATHER THAN FIVE DOCTYPES because the same grower
	is a competitor for labour, a partner in a packing shed and a
	possible acquisition in one season, and splitting them makes one
	organisation three records that drift apart.\n\nEVERY SCALE FIGURE
	HERE IS AN ESTIMATE and the result says so.

	THE HR GATE, not the field-ops one. A competitive register names
	other businesses and what this one thinks of them; it is a holding-
	company fact, and the picker holding the phone is entitled to their
	own work rather than to a rival's vulnerability windows.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("participant_name", participant_name),
		("participant_type", participant_type),
		("customer", customer),
		("strategic_plan", strategic_plan),
		("relationship_status", relationship_status),
		("industry_segment", industry_segment),
		("geography", geography),
		("crops", crops),
		("market_position", market_position),
		("market_share_pct", market_share_pct),
		("employee_count", employee_count),
		("estimated_revenue", estimated_revenue),
		("estimated_acreage", estimated_acreage),
		("strengths", strengths),
		("weaknesses", weaknesses),
		("key_assets", key_assets),
		("vulnerability_windows", vulnerability_windows),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return compintel_tools.create_market_participant(inner).data


# ── 243. update_acquisition_target ──────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("update_acquisition_target", limit=guard.WRITE_LIMIT, mutating=True)
def update_acquisition_target(
	user: str,
	acquisition_target=None,
	entity_name=None,
	market_participant=None,
	strategic_plan=None,
	status=None,
	action_level=None,
	strategic_fit_score=None,
	financial_health_score=None,
	synergy_score=None,
	cultural_fit_score=None,
	estimated_value=None,
	estimated_acquisition_cost=None,
	projected_revenue_uplift=None,
	projected_cost_savings=None,
	acreage=None,
	payback_period_years=None,
	irr_estimate=None,
	intergenerational_horizon_years=None,
	land_value_appreciation=None,
	water_rights_value=None,
	varietal_ip_value=None,
	infrastructure_value=None,
	identified_date=None,
	target_close_date=None,
	actual_close_date=None,
	rationale=None,
	recommendation=None,
	notes=None,
) -> dict:
	"""Move a target through the pipeline, or revise its scores and
	economics.\n\n`accretive_score` IS REFUSED.

	THE HR GATE, not the field-ops one. A competitive register names
	other businesses and what this one thinks of them; it is a holding-
	company fact, and the picker holding the phone is entitled to their
	own work rather than to a rival's vulnerability windows.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(
		compintel_tools.TARGET, acquisition_target, "acquisition_target", allowed
	)
	inner: dict = {"acquisition_target": named}
	for key, given in (
		("entity_name", entity_name),
		("market_participant", market_participant),
		("strategic_plan", strategic_plan),
		("status", status),
		("action_level", action_level),
		("strategic_fit_score", strategic_fit_score),
		("financial_health_score", financial_health_score),
		("synergy_score", synergy_score),
		("cultural_fit_score", cultural_fit_score),
		("estimated_value", estimated_value),
		("estimated_acquisition_cost", estimated_acquisition_cost),
		("projected_revenue_uplift", projected_revenue_uplift),
		("projected_cost_savings", projected_cost_savings),
		("acreage", acreage),
		("payback_period_years", payback_period_years),
		("irr_estimate", irr_estimate),
		("intergenerational_horizon_years", intergenerational_horizon_years),
		("land_value_appreciation", land_value_appreciation),
		("water_rights_value", water_rights_value),
		("varietal_ip_value", varietal_ip_value),
		("infrastructure_value", infrastructure_value),
		("identified_date", identified_date),
		("target_close_date", target_close_date),
		("actual_close_date", actual_close_date),
		("rationale", rationale),
		("recommendation", recommendation),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return compintel_tools.update_acquisition_target(inner).data


# ── 244. update_competitive_move ────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("update_competitive_move", limit=guard.WRITE_LIMIT, mutating=True)
def update_competitive_move(
	user: str,
	competitive_move=None,
	market_participant=None,
	move_type=None,
	strategic_plan=None,
	severity=None,
	observed_date=None,
	description=None,
	source=None,
	confidence=None,
	impact_assessment=None,
	market_impact_pct=None,
	revenue_impact=None,
	response_urgency=None,
	recommended_response=None,
	actual_response=None,
	response_date=None,
	outcome=None,
	notes=None,
) -> dict:
	"""Revise a move, or — the usual reason — record what was actually done
	about it and how it turned out.\n\nA RESPONSE DATE WITH NO RESPONSE
	IS REFUSED.

	THE HR GATE, not the field-ops one. A competitive register names
	other businesses and what this one thinks of them; it is a holding-
	company fact, and the picker holding the phone is entitled to their
	own work rather than to a rival's vulnerability windows.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(compintel_tools.MOVE, competitive_move, "competitive_move", allowed)
	inner: dict = {"competitive_move": named}
	for key, given in (
		("market_participant", market_participant),
		("move_type", move_type),
		("strategic_plan", strategic_plan),
		("severity", severity),
		("observed_date", observed_date),
		("description", description),
		("source", source),
		("confidence", confidence),
		("impact_assessment", impact_assessment),
		("market_impact_pct", market_impact_pct),
		("revenue_impact", revenue_impact),
		("response_urgency", response_urgency),
		("recommended_response", recommended_response),
		("actual_response", actual_response),
		("response_date", response_date),
		("outcome", outcome),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return compintel_tools.update_competitive_move(inner).data


# ── 245. update_market_participant ──────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("update_market_participant", limit=guard.WRITE_LIMIT, mutating=True)
def update_market_participant(
	user: str,
	market_participant=None,
	participant_name=None,
	participant_type=None,
	customer=None,
	strategic_plan=None,
	relationship_status=None,
	industry_segment=None,
	geography=None,
	crops=None,
	market_position=None,
	market_share_pct=None,
	employee_count=None,
	estimated_revenue=None,
	estimated_acreage=None,
	strengths=None,
	weaknesses=None,
	key_assets=None,
	vulnerability_windows=None,
	notes=None,
) -> dict:
	"""Revise a participant record — the estimates age, and an assessment
	nobody has touched in three seasons is describing a.

	THE HR GATE, not the field-ops one. A competitive register names
	other businesses and what this one thinks of them; it is a holding-
	company fact, and the picker holding the phone is entitled to their
	own work rather than to a rival's vulnerability windows.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another farm cannot be mapped by
	watching which error comes back.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(
		compintel_tools.PARTICIPANT, market_participant, "market_participant", allowed
	)
	inner: dict = {"market_participant": named}
	for key, given in (
		("participant_name", participant_name),
		("participant_type", participant_type),
		("customer", customer),
		("strategic_plan", strategic_plan),
		("relationship_status", relationship_status),
		("industry_segment", industry_segment),
		("geography", geography),
		("crops", crops),
		("market_position", market_position),
		("market_share_pct", market_share_pct),
		("employee_count", employee_count),
		("estimated_revenue", estimated_revenue),
		("estimated_acreage", estimated_acreage),
		("strengths", strengths),
		("weaknesses", weaknesses),
		("key_assets", key_assets),
		("vulnerability_windows", vulnerability_windows),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return compintel_tools.update_market_participant(inner).data


# ── 246. list_tasks_by_location ─────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_tasks_by_location", limit=guard.READ_LIMIT)
def list_tasks_by_location(
	user: str,
	location_filter=None,
	skill=None,
	task_type=None,
	urgency=None,
	company=None,
	limit=None,
) -> dict:
	"""One trip's worth of work per place: what this worker holds and what they
	could take, grouped by where it is.

	v0.124.0. THE TOOL SHIPPED IN v0.122.0 AND THE ROUTE DID NOT — the pass
	that mounted seventy-two methods a release later was written against the
	HACCP and traceability registers and this one sits in `fieldwork`, so it
	was not in the sweep. It is the only method added since v0.116.0, outside
	the strategy register, that a handset still could not reach.

	OPEN ON ENROLMENT, AND IT COULD NOT HONESTLY BE ANYTHING ELSE. This is a
	third reader of `list_my_tasks` and `list_available_for_me` — both already
	on this surface, both open — and it answers for the caller and nobody
	else. A gate here would close a screen onto the caller's own work while
	leaving the two calls it is assembled from wide open, which refuses
	nothing and only costs a trip.

	`worker_id` IS NOT ACCEPTED, for the reason `list_my_tasks` does not
	accept `employee`: the tool resolves the Employee from the authenticated
	user, and an account that can name somebody else in a request body is not
	scoped to anything. `guard.endpoint` injects the caller at `user` and
	`routes.py` drops the key even when a body carries it.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company` refuses an
	entity this caller cannot reach rather than quietly answering for a
	different one, and the tool's own `_company_for` falls back to the
	worker's preferred entity when nothing is named.
	"""
	allowed = guard.require_scope(user)
	wanted = guard.require_company(user, company, allowed)
	inner: dict = {"company": wanted} if wanted else {}
	for key, given in (
		("location_filter", location_filter),
		("skill", skill),
		("task_type", task_type),
		("urgency", urgency),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return fieldwork.list_tasks_by_location(inner).data


# ── 247. list_strategic_plans ───────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_strategic_plans", limit=guard.READ_LIMIT)
def list_strategic_plans(
	user: str,
	company=None,
	status=None,
	crop=None,
	limit=None,
) -> dict:
	"""The strategy register for this entity, newest first.

	THE HR GATE, not the field-ops one, and for the same reason the
	competitive registers carry it: a strategic plan is a holding-company
	document — the vision, the SWOT, the exit strategy — and the picker
	holding the phone is entitled to their own work rather than to what
	the owners intend to do with the business.

	SCOPED BY THE COMPANY IT IS ASKED FOR: `guard.require_company`
	refuses an entity this caller cannot reach, and the tool filters on
	the one that survives.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	for key, given in (
		("status", status),
		("crop", crop),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return strategy_tools.list_strategic_plans(inner).data


# ── 248. get_strategic_plan ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_strategic_plan", limit=guard.READ_LIMIT)
def get_strategic_plan(
	user: str,
	strategic_plan=None,
) -> dict:
	"""One plan in full, with the objectives under it and their KPI state.

	THE HR GATE. See `list_strategic_plans` — this is the same register
	read one document at a time, and the detail is the part a rival's
	handset would most want.

	SCOPED ON THE DOCNAME: `guard.require_scoped_doc` refuses a document
	belonging to an entity this caller cannot reach, and refuses it as
	NOT FOUND so the docnames of another entity cannot be mapped by
	watching which error comes back.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(strategy_tools.PLAN, strategic_plan, "strategic_plan", allowed)
	return strategy_tools.get_strategic_plan({"strategic_plan": named}).data


# ── 249. list_strategic_objectives ──────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_strategic_objectives", limit=guard.READ_LIMIT)
def list_strategic_objectives(
	user: str,
	company=None,
	strategic_plan=None,
	status=None,
	due_from=None,
	due_to=None,
	limit=None,
) -> dict:
	"""The objectives under the plans, with the overdue ones named.

	THE HR GATE, as with the plan itself. An objective carries the KPI
	target and what was actually measured against it, which is a
	statement about how the business is doing rather than about the
	caller's own work.

	THE PLAN FILTER IS SCOPED SEPARATELY when one is named, so a docname
	from another entity narrows nothing and reads as NOT FOUND rather
	than silently returning that entity's objectives.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	if strategic_plan not in (None, ""):
		inner["strategic_plan"] = guard.require_scoped_doc(
			strategy_tools.PLAN, strategic_plan, "strategic_plan", allowed
		)
	for key, given in (
		("status", status),
		("due_from", due_from),
		("due_to", due_to),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given
	return strategy_tools.list_strategic_objectives(inner).data


# ── 250. get_strategic_objective ────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_strategic_objective", limit=guard.READ_LIMIT)
def get_strategic_objective(
	user: str,
	strategic_objective=None,
) -> dict:
	"""One objective: its KPI target, what was measured, and when.

	THE HR GATE, and SCOPED ON THE DOCNAME the same way
	`get_strategic_plan` is — a refusal reads as NOT FOUND so another
	entity's docnames cannot be mapped from the error.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(
		strategy_tools.OBJECTIVE, strategic_objective, "strategic_objective", allowed
	)
	return strategy_tools.get_strategic_objective({"strategic_objective": named}).data


# ── 251. create_strategic_plan ──────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_strategic_plan", limit=guard.WRITE_LIMIT, mutating=True)
def create_strategic_plan(
	user: str,
	company=None,
	plan_name=None,
	crop=None,
	status=None,
	timeframe=None,
	previous_version=None,
	effective_date=None,
	retired_date=None,
	description=None,
	vision=None,
	mission=None,
	values_text=None,
	swot=None,
	porters_five_forces=None,
	sustainable_advantage=None,
	analogous_games=None,
	grand_strategy=None,
	business_strategy=None,
	command_structure=None,
	functional_tactics=None,
	validation_control=None,
	exit_strategy=None,
	notes=None,
) -> dict:
	"""Open a strategic plan for this entity.

	THE HR GATE ON THE WRITE TOO. A plan is superseded rather than
	rewritten — `previous_version` is how a plan points at the one it
	replaces — so what this creates is a document the business is
	measured against afterwards, and that is not a picker's call.

	SCOPED BY THE COMPANY IT IS ASKED FOR, and `previous_version` is
	scoped separately when given so a plan cannot be made to supersede
	another entity's.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	if previous_version not in (None, ""):
		inner["previous_version"] = guard.require_scoped_doc(
			strategy_tools.PLAN, previous_version, "previous_version", allowed
		)
	for key, given in (
		("plan_name", plan_name),
		("crop", crop),
		("status", status),
		("timeframe", timeframe),
		("effective_date", effective_date),
		("retired_date", retired_date),
		("description", description),
		("vision", vision),
		("mission", mission),
		("values_text", values_text),
		("swot", swot),
		("porters_five_forces", porters_five_forces),
		("sustainable_advantage", sustainable_advantage),
		("analogous_games", analogous_games),
		("grand_strategy", grand_strategy),
		("business_strategy", business_strategy),
		("command_structure", command_structure),
		("functional_tactics", functional_tactics),
		("validation_control", validation_control),
		("exit_strategy", exit_strategy),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return strategy_tools.create_strategic_plan(inner).data


# ── 252. update_strategic_plan ──────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("update_strategic_plan", limit=guard.WRITE_LIMIT, mutating=True)
def update_strategic_plan(
	user: str,
	strategic_plan=None,
	plan_name=None,
	crop=None,
	status=None,
	timeframe=None,
	previous_version=None,
	effective_date=None,
	retired_date=None,
	description=None,
	vision=None,
	mission=None,
	values_text=None,
	swot=None,
	porters_five_forces=None,
	sustainable_advantage=None,
	analogous_games=None,
	grand_strategy=None,
	business_strategy=None,
	command_structure=None,
	functional_tactics=None,
	validation_control=None,
	exit_strategy=None,
	notes=None,
) -> dict:
	"""Revise a plan in place.

	THE HR GATE, and SCOPED ON THE DOCNAME — both the plan being revised
	and any `previous_version` it is made to point at, so neither can
	reach out of the caller's entities.

	PASS ONLY WHAT CHANGES. Omitted fields are left alone.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(strategy_tools.PLAN, strategic_plan, "strategic_plan", allowed)
	inner: dict = {"strategic_plan": named}
	if previous_version not in (None, ""):
		inner["previous_version"] = guard.require_scoped_doc(
			strategy_tools.PLAN, previous_version, "previous_version", allowed
		)
	for key, given in (
		("plan_name", plan_name),
		("crop", crop),
		("status", status),
		("timeframe", timeframe),
		("effective_date", effective_date),
		("retired_date", retired_date),
		("description", description),
		("vision", vision),
		("mission", mission),
		("values_text", values_text),
		("swot", swot),
		("porters_five_forces", porters_five_forces),
		("sustainable_advantage", sustainable_advantage),
		("analogous_games", analogous_games),
		("grand_strategy", grand_strategy),
		("business_strategy", business_strategy),
		("command_structure", command_structure),
		("functional_tactics", functional_tactics),
		("validation_control", validation_control),
		("exit_strategy", exit_strategy),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return strategy_tools.update_strategic_plan(inner).data


# ── 253. create_strategic_objective ─────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("create_strategic_objective", limit=guard.WRITE_LIMIT, mutating=True)
def create_strategic_objective(
	user: str,
	company=None,
	strategic_plan=None,
	objective=None,
	status=None,
	due_date=None,
	owner_role=None,
	kpi_metric=None,
	kpi_target=None,
	kpi_actual=None,
	measured_on=None,
	notes=None,
) -> dict:
	"""Add an objective under a plan, with the KPI it is measured by.

	THE HR GATE, and THE PLAN IS SCOPED ON ITS DOCNAME so an objective
	cannot be filed under another entity's strategy.

	`owner_role` IS A ROLE RATHER THAN A PERSON, which is the tool's own
	choice and travels unchanged: an objective outlives whoever held the
	job when it was written.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	entity = guard.require_company(user, company, allowed) or (allowed[0] if allowed else "")
	inner: dict = {"company": entity}
	if strategic_plan not in (None, ""):
		inner["strategic_plan"] = guard.require_scoped_doc(
			strategy_tools.PLAN, strategic_plan, "strategic_plan", allowed
		)
	for key, given in (
		("objective", objective),
		("status", status),
		("due_date", due_date),
		("owner_role", owner_role),
		("kpi_metric", kpi_metric),
		("kpi_target", kpi_target),
		("kpi_actual", kpi_actual),
		("measured_on", measured_on),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return strategy_tools.create_strategic_objective(inner).data


# ── 254. update_strategic_objective ─────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("update_strategic_objective", limit=guard.WRITE_LIMIT, mutating=True)
def update_strategic_objective(
	user: str,
	strategic_objective=None,
	strategic_plan=None,
	objective=None,
	status=None,
	due_date=None,
	owner_role=None,
	kpi_metric=None,
	kpi_target=None,
	kpi_actual=None,
	measured_on=None,
	notes=None,
) -> dict:
	"""Revise an objective — most often to record what the KPI actually
	came in at.

	THE HR GATE, and BOTH DOCNAMES ARE SCOPED: the objective being
	revised, and the plan it is being moved to when one is named.

	PASS ONLY WHAT CHANGES. Omitted fields are left alone.
	"""
	personnel.require_hr_role()
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(
		strategy_tools.OBJECTIVE, strategic_objective, "strategic_objective", allowed
	)
	inner: dict = {"strategic_objective": named}
	if strategic_plan not in (None, ""):
		inner["strategic_plan"] = guard.require_scoped_doc(
			strategy_tools.PLAN, strategic_plan, "strategic_plan", allowed
		)
	for key, given in (
		("objective", objective),
		("status", status),
		("due_date", due_date),
		("owner_role", owner_role),
		("kpi_metric", kpi_metric),
		("kpi_target", kpi_target),
		("kpi_actual", kpi_actual),
		("measured_on", measured_on),
		("notes", notes),
	):
		if given not in (None, ""):
			inner[key] = given
	return strategy_tools.update_strategic_objective(inner).data


# ── 255. get_crew_overview ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_crew_overview", limit=guard.READ_LIMIT)
def get_crew_overview(user: str, company=None) -> dict:
	"""The farm today: every department this caller may see, its open crews,
	and who is standing in each of them.

	THE DISPATCH GATE, ON A READ, AND THAT IS A DEPARTURE WORTH ARGUING.
	The rule on this surface is that reads open on enrolment, because the
	reads are the CALLER'S OWN WORK — their tasks, their shift, the
	re-entry interval on the block they are about to walk into. This is not
	that. It names other people, where they are, who they are under and how
	many buckets they have picked, which is the crew roster of the whole
	department seen from outside. A picker's own day is answered by
	list_my_tasks and get_shift_crew_timeline and neither of them changes.

	IT IS NOT HR-GATED EITHER, AND THAT IS THE OTHER HALF OF THE SAME
	DECISION. Running the crew IS the Foreman's job on this farm — there is
	no personnel office to refer them to — so a gate that sent them looking
	for one would stop the work rather than protect anybody. The competitive
	and strategy registers are HR-gated because they are what the OWNERS
	intend to do with the business; a crew list is what the foreman is
	holding a phone to find out.

	THE DEPARTMENT SCOPE IS APPLIED INSIDE THE TOOL and is a second
	restriction on top of the entity scope, not a substitute for it. A
	Foreman sees their own department and its children; a Farm Manager sees
	every department in the entities they may reach. An account with no
	linked Employee, or an Employee with no department, gets an EMPTY
	overview and the sentence saying which — never the whole farm.
	"""
	guard.require_dispatch_role(user, "the crew overview")
	allowed = guard.require_scope(user)

	inner: dict = {}
	named = guard.require_company(user, company, allowed)
	if named:
		inner["company"] = named

	result = crew_view.get_crew_overview(inner)
	data = result.data
	# The belt to the braces, as everywhere else on this transport: the tool
	# already filtered on the caller's entities, and this drops any crew that
	# reached the payload through a path nobody thought about.
	for department in data.get("departments") or []:
		department["crews"] = guard.scoped(department.get("crews") or [], allowed)
	return data


# ── 255b. list_active_shifts ─────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("list_active_shifts", limit=guard.READ_LIMIT)
def list_active_shifts(
	user: str,
	company=None,
	department=None,
	stale_only=None,
	stale_after_hours=None,
	limit=None,
) -> dict:
	"""Every shift running right now, one row each — the supervisor's glance.

	THE SAME DISPATCH GATE AS THE OVERVIEW, AND THE SAME ARGUMENT. It names
	other people's crews, where they are and who has them, which is not
	the caller's own work — and it is not HR-gated either, because
	running the crew IS the Foreman's job on this farm.

	IT IS THE LIGHT READ AND THAT IS WHY IT IS A SEPARATE ROUTE. A phone
	polling "what is running" every couple of minutes should not be
	pulling every worker on every crew with their current task and their
	bucket count, which is what `get_crew_overview` returns. Both go
	through one shared read inside the tool, so the two cannot come to
	disagree about which shifts are open or who may see them.

	`stale_only` IS THE RUNAWAY WORKLIST — the crews open past the
	threshold, which are the shifts keeping their whole crew off every
	other roster. `end_stale_shift` closes one.
	"""
	guard.require_dispatch_role(user, "the active shift board")
	allowed = guard.require_scope(user)

	inner: dict = {}
	named = guard.require_company(user, company, allowed)
	if named:
		inner["company"] = named
	for key, given in (
		("department", department),
		("stale_only", stale_only),
		("stale_after_hours", stale_after_hours),
		("limit", limit),
	):
		if given not in (None, ""):
			inner[key] = given

	data = crew_view.list_active_shifts(inner).data
	# The belt to the braces, as everywhere else on this transport.
	data["shifts"] = guard.scoped(data.get("shifts") or [], allowed)
	return data


# ── 256. get_worker_detail ───────────────────────────────────────────────
@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_worker_detail", limit=guard.READ_LIMIT)
def get_worker_detail(user: str, employee=None) -> dict:
	"""One worker's shift, their day's jobs and their compliance standing.

	THE SAME DISPATCH GATE AS THE OVERVIEW ABOVE, AND FOR THE SAME REASON —
	this is the drill-down from it, and a gate on the list that the detail
	did not carry would be no gate at all.

	WHAT IT WITHHOLDS IS THE SECURITY CONTENT AND IT IS DONE IN THE TOOL.
	`crew_view.WORKER_FIELDS` is the whole of what this says about a person:
	name, job title, department, company, status, start date and photo. No
	date of birth, no home address, no bank account, no wage, and from the
	I-9 register NOTHING BUT THE STATUS WORD — not the documents, not the
	numbers on them. A foreman needs to know that somebody's authorisation
	needs re-verifying; the document it would be checked against is
	get_i9_form's to hand over and get_i9_form is gated on the personnel
	roles.

	THE DOCNAME IS SCOPED TWICE. `require_scoped_doc` proves the Employee is
	in an entity this account may reach — an Employee of another company
	reads as NOT FOUND rather than as refused, so the register cannot be
	mapped from the error — and the tool then refuses a worker whose
	DEPARTMENT is outside the caller's scope, which is the check the entity
	scope cannot make on a single-company farm.
	"""
	guard.require_dispatch_role(user, "another worker's detail")
	allowed = guard.require_scope(user)
	named = _employee_argument(employee, allowed)
	return crew_view.get_worker_detail({"employee": named}).data


# ── 257. end_stale_shift ─────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("end_stale_shift", limit=guard.WRITE_LIMIT, mutating=True)
def end_stale_shift(
	user: str,
	shift=None,
	farm_shift=None,
	end_datetime=None,
	ended_at=None,
	reason=None,
	stale_after_hours=None,
	supervisor_signature_file_token=None,
	reviewed_on=None,
	foreman_notes=None,
) -> dict:
	"""Close a shift nobody clocked out of, release its crew, and pay them.

	THE DISPATCH GATE, because this ends somebody ELSE'S shift. Every other
	write on this transport that reaches another person's record carries it,
	and this one reaches a whole crew's: it sets an end time on the shift,
	a `left_at` on every crew member still on it, and one Attendance row
	each. A picker cannot close the crew they are standing in.

	IT IS FENCED SO IT CANNOT BECOME THE ORDINARY CLOSE. The shift must
	still be OPEN and must have been open longer than the staleness
	threshold — sixteen hours by default, past the end of anything anybody
	works — and a younger shift is refused BY NAME with `end_shift` named
	as the tool for it. `stale_after_hours` lowers the threshold
	deliberately; it is an argument rather than a setting because the number
	is a judgement about one shift.

	THE SIGNATURE IS OPTIONAL AND ITS ABSENCE COMES BACK IN THE ANSWER.
	`supervisor_review_owed` is true when none was passed, and the shift
	carries no invented attestation — see `tools/crew_view.py` for the whole
	argument. Where the supervisor IS there, pass the token on this call:
	once the shift is closed, `end_shift` will not reopen it to add one.

	`end_datetime` IS REQUIRED AND IS NOT DEFAULTED TO NOW. A crew that
	stopped at 14:00 on Tuesday, clocked out on Thursday morning by a
	default, would be paid for forty hours.
	"""
	guard.require_dispatch_role(user, "closing another crew's shift")
	allowed = guard.require_scope(user)
	named = guard.require_scoped_doc(FARM_SHIFT, shift or farm_shift, "shift", allowed)

	inner: dict = {"shift": named}
	for key, given in (
		("end_datetime", end_datetime or ended_at),
		("reason", reason),
		("stale_after_hours", stale_after_hours),
		("supervisor_signature_file_token", supervisor_signature_file_token),
		("reviewed_on", reviewed_on),
		("foreman_notes", foreman_notes),
	):
		if given not in (None, ""):
			inner[key] = given

	return crew_view.end_stale_shift(inner).data

# SPDX-License-Identifier: MIT
"""One call for a new hire: the Employee, their paperwork, their phone, their first day.

v0.17.1. A new picker arriving in July needs six things to exist before they can
be handed a phone and sent to a block, and every one of them is a different form:
an Employee record, an I-9 and a W-4 filed where payroll can find them, a photo
ID, a login scoped to the right entity, a credential, and the two tasks nobody
skips — safety training and camp orientation. Six forms, ten minutes, and the
step that gets missed is never the same one twice.

`onboard_employee` is one call that does all of it, and it is an ORCHESTRATOR
rather than an implementation: every step delegates to the tool that already owns
it, so the rules those tools enforce are enforced here too, because they are the
same code.

────────────────────────────────────────────────────────────────────────────
THE PAPERWORK GOES ON THE EMPLOYEE. NOT IN THE GOVERNANCE ARCHIVE.
────────────────────────────────────────────────────────────────────────────

THIS IS THE ONE THING TO GET RIGHT IN THIS FILE, and it was got wrong once
already. An I-9, a W-4 and a photograph of somebody's driver's licence are
PERSONAL RECORDS about one employee. They belong on that employee's own record,
as private attachments, reachable by whoever can already read the employee.

`attach_governance_document` files a NEW Governance Document — the register for
trust instruments, operating agreements, SOPs and certification paperwork: the
documents that describe the BUSINESS. Putting a worker's identity documents
there would file forty people's immigration paperwork into the archive an
auditor, an advisor or a family member browses, and it would do it while looking
tidy.

So every document here goes through `files.attach_file_to_document` with
`doctype="Employee"` and `is_private=1`, and there is a test that no path in this
module reaches the governance archive.

────────────────────────────────────────────────────────────────────────────
IT STOPS AT THE FIRST REAL REFUSAL, AND SAYS WHAT IT ALREADY DID
────────────────────────────────────────────────────────────────────────────

An orchestrator that half-completes is only dangerous if nobody can tell which
half. Every step is reported by name with what happened to it, so a run that
created the Employee and then failed to attach a W-4 says exactly that — and
re-running is safe, because each underlying tool is idempotent about the thing it
owns (`create_mobile_user` refuses a second account and takes `update_existing`;
`attach_file_to_document` refuses a duplicate file name on the same parent).

THE ONE STEP THAT IS ALLOWED TO FAIL QUIETLY IS THE TASKS. A safety-training task
that could not be raised because the dispatch doctypes are not installed must not
undo an employee's onboarding — it is reported in `skipped` and somebody raises
it by hand.

────────────────────────────────────────────────────────────────────────────
v0.18.1 CHANGED THE ORDER, AND THE ORDER WAS A BUG
────────────────────────────────────────────────────────────────────────────

Until v0.18.1 this created the Employee with `user_id` already filled in, and
THEN created the User. `Employee.user_id` is a Link to User, so on a real bench
Frappe validates it on insert and the very first step raised — an onboarding that
named an email could not complete at all. The standalone suite modelled that field
as plain Data and called it a pass; v0.18.1 models it as the Link it is, which is
what turned a live failure into a failing test.

So the order is now the only one that can work, and it is also the order somebody
would describe out loud:

  1. the Employee, with NO login on it yet
  2. the scoped mobile account, with its role, entities, grant and credential
  3. the LINK between them — `employee.link_employee_to_user`, which is also what
     an operator calls on its own when the person already existed
  4. the login QR, on request
  5. the first-day tasks

Every step still delegates. Step 1 goes through `employee.create_employee`, so the
twenty-two-field allowlist, the schema checks, the compliance defaults and the
mandatory-field message are the same code an operator gets calling that tool
directly — including the v0.46.1 fix, which is why `onboard_employee` was refusing
every hire on a migrated site and is not any more.

IT IS IDEMPOTENT, AND THAT IS A PROPERTY OF THE LOOKUPS RATHER THAN OF A FLAG.
Called twice with the same arguments, the second run finds the Employee (by login,
then by name and company), finds the User, finds the link already made, and
reports every one of them as reused. Nothing is duplicated and nothing is
rewritten — the one thing a second run does differently is mint a fresh QR, and
only when asked, because a QR is a credential and re-issuing one is the point of
asking for it twice.
"""

from __future__ import annotations

import frappe

from .. import compat
from ..args import as_bool, as_date, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import dispatch, files, i9, mobile
from . import employee as employee_tool
from . import housing as housing_tool
from . import shifts as shift_tool
from . import w4 as w4_tool
from .housing import EMPLOYEE, hr_installed

#: The paperwork this tool knows how to file, and what each is called on the
#: Employee's attachment list. Keys are what a caller passes; the label is what
#: somebody reading the Employee's file sees six months later, when "scan.pdf"
#: would tell them nothing.
DOCUMENT_KINDS = {
	"i9": "Form I-9 — employment eligibility verification",
	"w4": "Form W-4 — employee withholding certificate",
	"photo_id": "Photo identification",
	"direct_deposit": "Direct deposit authorisation",
	"signed_offer": "Signed offer letter",
}

#: The two tasks nobody skips for a new arrival, raised only when asked for.
#: Both demand a signature, because "I was told about the machinery" is a claim
#: whose entire value is that somebody attested to it.
FIRST_DAY_TASKS = (
	{
		"key": "safety_training",
		"task_name": "Safety training — {name}",
		"task_type": "Training",
		"evidence_required": {"signature": True, "findings_text": True},
		"notes": (
			"Walk the new arrival through machinery, chemical handling, heat illness and the "
			"reporting route. Their signature is the record that it happened."
		),
	},
	{
		"key": "camp_orientation",
		"task_name": "Camp orientation — {name}",
		"task_type": "Training",
		"evidence_required": {"signature": True, "findings_text": True},
		"notes": (
			"Show them the unit, the water, the laundry, the egress routes and who to call. "
			"Required before first occupancy."
		),
	},
)


def onboard_employee(args: dict) -> ToolResult:
	"""Create a new hire end to end: record, paperwork, mobile access, first tasks.

	v0.94.0: IT NOW CARRIES A GATE, AND IT HAD NONE. This is the one-call version
	of `create_employee` + `create_i9_form` + `submit_w4` + housing + crew + login,
	and every one of those carries `require_hiring_role` individually — so the
	chained form was the widest door in the hiring surface precisely because it
	did the most. `require_hiring_role` rather than `require_hr_role` because the
	whole point of this release is that the foreman runs a hire; the point of the
	gate is that the shortcut is not wider than the long way round.
	"""
	if not hr_installed():
		raise ToolError(
			"this site has no Employee register, so there is nobody to onboard. Frappe HR ships "
			"the Employee doctype — install it, or create the person's records by hand. Nothing "
			"was created."
		)

	full_name = as_str(args, "full_name", required=True).strip()
	if " " not in full_name:
		raise ToolError(
			f"full_name is {full_name!r}. An I-9, a payroll register and a dispatch board all name "
			"the same person, and a record carrying one word names nobody findable. Nothing was "
			"created."
		)
	actor = employee_tool.require_hiring_role()
	company = employee_tool.require_company_scope(
		actor, resolve_company(as_str(args, "company"), required=True)
	)
	email = as_str(args, "email").strip().lower()

	report = {
		"full_name": full_name,
		"company": company,
		"steps": [],
		"skipped": [],
		"employee": None,
		"i9_form": None,
		"documents": [],
		"mobile": None,
		"link": None,
		"qr": None,
		"tasks": [],
		# v0.93.0. The three steps that used to be somebody's next four calls.
		"w4_form": None,
		"housing": None,
		"crew": None,
	}

	employee = _employee(args, full_name, company, report)
	report["employee"] = employee

	_i9_form(args, employee, company, report)
	_w4_form(args, employee, company, report)
	_paperwork(args, employee, report)
	_housing(args, employee, full_name, company, report)

	if email:
		_mobile_access(args, email, full_name, company, employee, report)
		# THE LINK RUNS WHETHER OR NOT THE ACCOUNT WAS CREATED HERE. A second run
		# finds `create_mobile_user` refusing an account it already made, which is
		# right — and the link is the step that was missing when v0.18.0 could not
		# serve a task board, so it must not be collateral damage from that
		# refusal. It is idempotent on its own.
		_link(args, email, employee, report)
		_qr(args, email, report)
	else:
		report["skipped"].append(
			{
				"step": "mobile access",
				"reason": (
					"no email was given, and a Frappe User is named by one. The Employee exists and "
					"can be given a login later with create_mobile_user, then linked to this record "
					"with link_employee_to_user."
				),
			}
		)

	_crew(args, employee, report)

	if as_bool(args, "first_day_tasks", False):
		_first_day(args, employee, full_name, company, report)

	# v0.214.0. A new hire with no photo gets the badge photo task. Never fatal,
	# and off with the `badge_photo_auto_request` flag.
	from .. import badge_photo

	report["badge_photo_task"] = badge_photo.auto_request(employee)

	linked = bool((report["link"] or {}).get("user_id"))
	return ToolResult(
		data={
			**report,
			"note": (
				"Every document was filed PRIVATELY ON THE EMPLOYEE RECORD, not in the governance "
				"archive: an I-9 and a photograph of somebody's licence are personal records about "
				"one person, and the archive is where the documents describing the BUSINESS live. "
				"Whoever can read the Employee can read them; nobody else can."
			),
			"next_step": _next_step(report, linked),
			# WHAT DID NOT HAPPEN, AS A LIST RATHER THAN A SENTENCE. `next_step`
			# names ONE thing on purpose; a single-pass onboarding that touches
			# nine registers can leave several undone, and an orchestrator whose
			# gaps are only readable by diffing `steps` against what was asked for
			# is an orchestrator whose gaps nobody reads.
			"incomplete": [entry["step"] for entry in report["skipped"]],
		},
		summary=(
			f"onboarded {full_name} at {company}: employee {employee}"
			+ (f", {len(report['documents'])} document(s)" if report["documents"] else "")
			+ (", W-4 filed" if report["w4_form"] else "")
			+ (", housed" if report["housing"] else "")
			+ (", on a crew" if report["crew"] else "")
			+ (", mobile access" if report["mobile"] else "")
			+ (", linked" if linked else "")
			+ (", QR issued" if report["qr"] else "")
			+ (f", {len(report['tasks'])} first-day task(s)" if report["tasks"] else "")
			+ (f"; {len(report['skipped'])} step(s) NOT done" if report["skipped"] else "")
		),
		docstatus_delta="none → 0 (Employee created)",
	)


def _crew(args: dict, employee: str, report: dict) -> None:
	"""Roster them onto a shift that is already running.

	LAST, AND AFTER THE LOGIN, because a crew row is the only step here that puts
	somebody on the clock. Everything before it is paperwork that can be finished
	at a desk; this one says a person is working right now, and doing it before
	the account exists produces a worker on a crew who cannot be handed the phone
	the crew is using.

	It delegates for the usual reason and one specific to shifts:
	`add_worker_to_shift` refuses a second open shift for the same person, which
	no check written here could see — a worker rostered onto a second crew is
	invisible from the first one's rows.
	"""
	shift = as_str(args, "shift")
	if not shift:
		return
	payload = {"shift": shift, "employee": employee}
	for key in ("role", "joined_at"):
		value = as_str(args, f"crew_{key}") or as_str(args, key)
		if value:
			payload[key] = value
	try:
		result = shift_tool.add_worker_to_shift(payload)
		report["crew"] = {
			"shift": result.data.get("shift") or shift,
			"crew_size": result.data.get("crew_size"),
			"role": payload.get("role") or "Worker",
		}
		report["steps"].append({"step": "crew", "action": "rostered", "name": shift})
	except Exception as exc:
		report["skipped"].append({"step": "crew", "reason": f"{type(exc).__name__}: {exc}"})


def _next_step(report: dict, linked: bool) -> str:
	"""The one thing left to do in the ENROLMENT chain, named — plus the W-4.

	THE W-4 IS APPENDED RATHER THAN PUT IN FRONT, and that is a deliberate
	restraint. This field has meant "the next step towards a working phone" since
	the tool shipped, and callers read it that way; a missing W-4 is a different
	kind of gap — it produces a WRONG NUMBER rather than a missing capability,
	because somebody with no phone cannot pick and somebody with no W-4 is paid at
	the default and finds out in April. Two different gaps, so two sentences,
	rather than one of them silently displacing the other in a field somebody
	already parses.
	"""
	return _enrolment_step(report, linked) + _withholding_step(report)


def _withholding_step(report: dict) -> str:
	if report["w4_form"] or not any(entry["step"] == "w4_form" for entry in report["skipped"]):
		return ""
	return (
		" AND: submit_w4 files this person's withholding elections. Until it does, payroll "
		"withholds at the default for them — list_employees_missing_w4 is the register."
	)


def _enrolment_step(report: dict, linked: bool) -> str:
	if not report["mobile"] and not linked:
		return "create_mobile_user gives this person a login when they need one."
	if not linked:
		return (
			"link_employee_to_user connects this Employee to the login — until it does, the "
			"mobile methods have no person to scope a task board to."
		)
	if not report["qr"]:
		return "generate_mobile_login_qr for this account hands over the credential."
	return (
		"Show the QR to the phone. `endpoint` in the qr block is the first URL it will call — "
		"curl it before handing the phone to anybody standing in an orchard."
	)


# ── 1. the Employee record ──────────────────────────────────────────────────
def _employee(args: dict, full_name: str, company: str, report: dict) -> str:
	"""Find or create the Employee. Never a second one for the same person.

	An existing Employee is REUSED rather than duplicated. Two Employee records
	for one person is the failure that puts somebody on the dispatch board twice
	and in the payroll register once, and it is far easier to make than to find.

	THREE LOOKUPS BEFORE ANYTHING IS CREATED, and they are what makes a re-run
	idempotent: the docname a caller named, then the login (which the previous run
	linked), then the name and hiring company together. The third is the one that
	covers a re-run with no email at all, where the first two have nothing to
	match on.

	CREATION DELEGATES to `create_employee`, so the twenty-two-field allowlist, the
	Link and Select checks against this site's own schema, the starting values for
	the three compliance statuses this app installs as mandatory, and the
	mandatory-field message are the same code an operator gets calling that tool by
	hand. NO
	`user_id` IS PASSED — the login does not exist yet, and `Employee.user_id` is
	a Link that Frappe validates. The link is step 3.
	"""
	existing = as_str(args, "employee")
	if existing:
		if not frappe.db.exists(EMPLOYEE, existing):
			raise ToolError(f"no Employee called {existing!r} on this site. Nothing was created.")
		report["steps"].append({"step": "employee", "action": "reused", "name": existing})
		return existing

	email = as_str(args, "email").strip().lower()
	if email and compat.has_field(EMPLOYEE, "user_id"):
		linked = frappe.db.get_value(EMPLOYEE, {"user_id": email}, "name")
		if linked:
			report["steps"].append({"step": "employee", "action": "reused", "name": str(linked)})
			return str(linked)

	same = frappe.db.get_value(EMPLOYEE, {"employee_name": full_name, "company": company}, "name")
	if same:
		report["steps"].append({"step": "employee", "action": "reused", "name": str(same)})
		return str(same)

	# v0.54.0 added `department`, `employment_type` and `branch`. `designation`
	# has been here since the tool shipped and the other three had not, which
	# made "what do they do" answerable off an onboarding and "what are they
	# hired AS, where, and under whom" not — and `employment_type` is the one
	# that decides whether somebody is Seasonal, which is the fact an H-2A
	# roster, an ACA hours count and a piece-rate wage statement all turn on.
	# All four go through `create_employee`'s allowlist and its Link checks, so
	# a value that names no record on this site is refused there with the site's
	# own choices listed, not written blind here.
	payload = {
		"employee_name": full_name,
		"company": company,
		"status": "Active",
		"date_of_joining": as_str(args, "date_of_joining"),
		"date_of_birth": as_str(args, "date_of_birth"),
		"gender": as_str(args, "gender"),
		"designation": as_str(args, "designation"),
		"department": as_str(args, "department"),
		"employment_type": as_str(args, "employment_type"),
		"branch": as_str(args, "branch"),
		"cell_number": as_str(args, "phone"),
		"personal_email": email,
	}
	result = employee_tool.create_employee({key: value for key, value in payload.items() if value})
	name = result.data["employee"]
	report["steps"].append({"step": "employee", "action": "created", "name": name})
	return name


# ── 1b. the structured I-9 ──────────────────────────────────────────────────
def _i9_form(args: dict, employee: str, company: str, report: dict) -> None:
	"""Create a Draft I-9 Form if the I-9 Form doctype exists on this site.

	v0.27.0. `onboard_employee` auto-creates a structured I-9 alongside the
	Employee, so the I-9 workflow starts from onboarding rather than requiring a
	separate create_i9_form call. If create_i9_form is not enabled on the settings,
	or the doctype does not exist, this is silently skipped — the paperwork step
	still attaches any I-9 file attachment the caller supplied.

	NOT FATAL. A structured I-9 that could not be created must not undo an
	onboarding that otherwise worked.
	"""
	if not compat.doctype_exists("I-9 Form"):
		return

	existing = frappe.db.get_value("I-9 Form", {"employee": employee, "status": ["!=", "Destroyed"]}, "name")
	if existing:
		report["steps"].append({"step": "i9_form", "action": "reused", "name": str(existing)})
		report["i9_form"] = str(existing)
		return

	hire_date = as_date(args, "date_of_joining") or str(frappe.utils.today())
	try:
		result = i9.create_i9_form({"employee": employee, "company": company, "hire_date": hire_date})
		report["i9_form"] = result.data.get("name")
		report["steps"].append({"step": "i9_form", "action": "created", "name": result.data.get("name")})
	except Exception as exc:
		report["skipped"].append({"step": "i9_form", "reason": f"{type(exc).__name__}: {exc}"})


# ── 1c. the structured W-4 ──────────────────────────────────────────────────
def _w4_form(args: dict, employee: str, company: str, report: dict) -> None:
	"""File the withholding elections as a W-4 Form, not only as a scanned page.

	v0.93.0, AND THE ASYMMETRY IT CLOSES. `_i9_form` has created a structured I-9
	since v0.27.0 while the W-4 could only arrive here as a PDF under
	`documents["w4"]` — a picture of a form, which nothing computes from. The
	payroll engine reads the ELECTIONS: filing status, the dependent counts, the
	extra withholding. A farm that onboarded forty pickers through this tool and
	attached forty W-4 scans still had forty people in
	`list_employees_missing_w4`, and the first payroll run withheld at the default
	for every one of them.

	So the scan and the elections are different facts and both are kept: this
	files the record, `_paperwork` still attaches the signed page beside it, and
	neither replaces the other — the signed page is what an IRS examiner asks to
	see and the record is what the engine computes from.

	NOT FATAL, and skipped LOUDLY. A W-4 that could not be filed must not undo an
	onboarding that otherwise worked, but it must also not vanish: the reason
	lands in `skipped`, the step name lands in `incomplete`, and
	`list_employees_missing_w4` finds the person either way.
	"""
	elections = args.get("w4")
	if not elections:
		if compat.doctype_exists(w4_tool.W4_FORM):
			report["skipped"].append(
				{
					"step": "w4_form",
					"reason": (
						"no `w4` elections were given, so nothing computes this person's "
						"withholding and the first payroll run uses the default. A scanned W-4 "
						"under documents['w4'] is the signed page and not the elections. "
						"submit_w4 files them later; list_employees_missing_w4 names everybody "
						"still in this state."
					),
				}
			)
		return
	if not isinstance(elections, dict):
		raise ToolError(
			'w4 must be an object like {"filing_status": "Married Filing Jointly", '
			'"dependents_under_17_count": 2}. Nothing further was done — the Employee record '
			f"({employee}) stands."
		)

	joined = as_date(args, "date_of_joining") or str(frappe.utils.today())
	payload = {
		"employee": employee,
		"company": company,
		# THE YEAR THE ELECTIONS APPLY TO, defaulted from the hire date rather
		# than from today. Somebody onboarded in December against a January start
		# is filing for the year they will be paid in, and a W-4 filed under the
		# wrong tax year is invisible to the engine that looks it up by year.
		"tax_year": elections.get("tax_year") or int(str(joined)[:4]),
		**{key: value for key, value in elections.items() if key != "tax_year"},
	}
	try:
		result = w4_tool.submit_w4(payload)
		report["w4_form"] = result.data.get("name")
		report["steps"].append({"step": "w4_form", "action": "created", "name": result.data.get("name")})
	except Exception as exc:
		report["skipped"].append({"step": "w4_form", "reason": f"{type(exc).__name__}: {exc}"})


# ── 2. the paperwork ────────────────────────────────────────────────────────
def _paperwork(args: dict, employee: str, report: dict) -> None:
	"""File each supplied document PRIVATELY on the Employee record.

	See the module docstring. `attach_file_to_document` with `doctype="Employee"`
	— never `attach_governance_document`, which files into the register an
	auditor and a family member browse.
	"""
	documents = args.get("documents") or {}
	if isinstance(documents, list):
		documents = {str(entry.get("kind") or ""): entry for entry in documents if isinstance(entry, dict)}
	if not isinstance(documents, dict):
		raise ToolError(
			'documents must be an object like {"i9": {"file_name": "...", "file_content": "<base64>"}} '
			f"or a list of objects each naming a `kind`. Known kinds: {', '.join(sorted(DOCUMENT_KINDS))}."
		)

	for kind, payload in documents.items():
		kind = str(kind).strip().lower()
		if kind not in DOCUMENT_KINDS:
			raise ToolError(
				f"{kind!r} is not a document kind this tool files. The five are: "
				f"{', '.join(sorted(DOCUMENT_KINDS))}. Nothing further was attached — the Employee "
				f"record ({employee}) and any earlier document stand."
			)
		if isinstance(payload, str):
			payload = (
				{"file_url": payload} if payload.startswith(("/", "http")) else {"file_content": payload}
			)
		if not isinstance(payload, dict):
			raise ToolError(f"documents[{kind!r}] must be an object naming a file.")

		inner = {
			"doctype": EMPLOYEE,
			"name": employee,
			"file_name": as_str(payload, "file_name") or f"{kind}-{employee}.pdf",
			# PRIVATE, NOT AN ARGUMENT. A public URL to somebody's I-9 is readable
			# by anyone who can guess it, and there is no version of this tool
			# where that is the caller's choice to make.
			"is_private": True,
		}
		for key in ("file_content", "file_url"):
			if payload.get(key):
				inner[key] = payload[key]

		result = files.attach_file_to_document(inner)
		report["documents"].append(
			{
				"kind": kind,
				"label": DOCUMENT_KINDS[kind],
				"file": result.data.get("file"),
				"file_name": result.data.get("file_name"),
				"sha256": result.data.get("sha256"),
				"is_private": True,
			}
		)
		report["steps"].append({"step": f"document:{kind}", "action": "attached"})


# ── 2b. the bed ─────────────────────────────────────────────────────────────
def _housing(args: dict, employee: str, full_name: str, company: str, report: dict) -> None:
	"""Put them in a unit, through the tool that owns occupancy.

	DELEGATES rather than writes, like every other step here, and the delegation
	is what matters: `create_housing_assignment` refuses an overlap, checks the
	unit against Oregon's lawful occupancy, and knows that somebody moving out on
	the 15th and somebody moving in on the 15th DID share the cabin that night.
	Writing the row here would be a second implementation of all three, and the
	one that got it wrong would be the one a camp actually used.

	THE DATE DEFAULTS TO THE HIRE DATE, not to today. A worker hired on Monday and
	onboarded in the office on Wednesday slept somewhere on Monday night, and an
	assignment starting Wednesday says the camp had a bed empty that it did not.
	"""
	unit = as_str(args, "housing_unit")
	if not unit:
		return
	payload = {
		"unit": unit,
		"employee": employee,
		"employee_name": full_name,
		"company": company,
		"assigned_date": as_date(args, "housing_assigned_date")
		or as_date(args, "date_of_joining")
		or str(frappe.utils.today()),
	}
	for key in ("housing_deduction_from_wages", "end_date", "notes"):
		value = args.get(key if key != "notes" else "housing_notes")
		if value not in (None, ""):
			payload[key if key != "notes" else "notes"] = value
	try:
		result = housing_tool.create_housing_assignment(payload)
		report["housing"] = result.data.get("name") or result.data.get("assignment")
		report["steps"].append({"step": "housing", "action": "assigned", "name": report["housing"]})
	except Exception as exc:
		report["skipped"].append({"step": "housing", "reason": f"{type(exc).__name__}: {exc}"})


# ── 3. the login and the credential ─────────────────────────────────────────
def _mobile_access(args: dict, email: str, full_name: str, company: str, employee: str, report: dict) -> None:
	"""Give them a scoped account, through the tool that owns that decision.

	`create_mobile_user` refuses an account with no entity access — see its
	docstring on why an unscoped mobile account is the least scoped account on the
	site — and this passes the hiring company as the default so the refusal never
	fires for a reason the caller did not cause.
	"""
	role = as_str(args, "role") or "Field Worker"
	entities = args.get("entity_access") or [company]
	try:
		result = mobile.create_mobile_user(
			{
				"email": email,
				"full_name": full_name,
				"role": role,
				"entity_access": entities,
				"preferred_company": as_str(args, "preferred_company") or company,
				"update_existing": as_bool(args, "update_existing", False),
				"notes": f"Onboarded with the Employee record {employee}.",
			}
		)
	except ToolError as exc:
		# A login that could not be created must not undo an Employee that could.
		report["skipped"].append({"step": "mobile access", "reason": str(exc)})
		return

	data = result.data
	report["mobile"] = {
		"user": data.get("user"),
		"role": data.get("role"),
		"entity_access": data.get("entity_access"),
		"grant": data.get("grant"),
		"employee": data.get("employee"),
		# THE SECRET IS NOT COPIED UP HERE. `create_mobile_user` returns it once
		# and says so; repeating it in an orchestrator's result would put a live
		# credential in a second place, in a payload somebody is much more likely
		# to paste into a chat window because it reads like a summary.
		"credential_note": (
			"A credential was issued. Run generate_mobile_login_qr for this user to hand it over — "
			"it is deliberately not repeated in this result."
			if data.get("api_key")
			else data.get("secret_note")
		),
	}
	report["steps"].append({"step": "mobile access", "action": "created", "name": data.get("user")})


# ── 3b. the link between the two ────────────────────────────────────────────
def _link(args: dict, email: str, employee: str, report: dict) -> None:
	"""Point the Employee at the login. THIS IS THE STEP v0.18.0 WAS MISSING.

	Every Farm Ops method scopes work by EMPLOYEE. A login with no Employee behind
	it gets `list_my_tasks` refusing it with "set user_id on their Employee record
	to this email address" — which is correct and was, until v0.18.1, unfixable
	from here. Without this step the other four produce a phone that enrols
	perfectly and then shows an empty screen with an error on it.

	`allow_unenrolled_user=true` because the account was created moments ago in
	step 2 and its grant may or may not have landed depending on what that step
	was allowed to do; refusing the link on those grounds would refuse it for a
	reason this orchestrator itself caused. The refusal is real and useful when an
	operator calls `link_employee_to_user` directly, which is where it belongs.

	NOT FATAL. An Employee that exists and a login that exists are both worth
	keeping when only the join between them failed — it is one call to repair, and
	`skipped` says which call.
	"""
	try:
		result = employee_tool.link_employee_to_user(
			{"employee": employee, "user_id": email, "allow_unenrolled_user": True}
		)
	except ToolError as exc:
		report["skipped"].append({"step": "link", "reason": str(exc)})
		return
	data = result.data
	report["link"] = {
		"employee": data.get("employee"),
		"user_id": data.get("user_id"),
		"action": data.get("action"),
		"farm_ops_ready": (data.get("linkage") or {}).get("farm_ops_ready"),
		"note": data.get("note"),
	}
	report["steps"].append({"step": "link", "action": data.get("action"), "name": data.get("user_id")})


# ── 3c. the credential, as something a phone can scan ───────────────────────
def _qr(args: dict, email: str, report: dict) -> None:
	"""The login card, on request only, and WITHOUT the plaintext beside it.

	`issue_qr` DEFAULTS FALSE. Minting a QR rotates the account's API secret, so a
	default-true would mean that re-running an onboarding to add a W-4 silently
	knocked a phone already in somebody's pocket offline. Asking for a credential
	is how you say you intend to hand one over.

	WHAT COMES BACK IS THE IMAGE AND NOT THE PAYLOAD. `generate_mobile_login_qr`
	returns both; the payload carries `api_secret` as readable text, and copying
	that into an orchestrator's summary would put a live credential in a second,
	much more pasteable place — which is the rule this file has followed since
	v0.17.1 and does not get an exception for being convenient. The PNG encodes the
	same secret, unavoidably, because that is what enrolment by QR IS; the
	difference is that nobody pastes a PNG into a chat window by accident.
	"""
	if not as_bool(args, "issue_qr", False):
		return
	try:
		result = mobile.generate_mobile_login_qr({"user": email, "url": as_str(args, "url")})
	except ToolError as exc:
		report["skipped"].append({"step": "qr", "reason": str(exc)})
		return
	data = result.data
	report["qr"] = {
		"png_base64": data.get("png_base64"),
		"mime_type": data.get("mime_type"),
		"bytes": data.get("bytes"),
		"pixels": data.get("pixels"),
		"expires_at": data.get("expires_at"),
		"endpoint": data.get("endpoint"),
		"grant": data.get("grant"),
		"security_note": data.get("security_note"),
		"payload_note": (
			"The scannable image only. The decoded payload — which carries the api_secret as "
			"readable text — is deliberately not repeated here; generate_mobile_login_qr returns "
			"it if you genuinely need it."
		),
	}
	report["steps"].append({"step": "qr", "action": "issued", "name": email})


# ── 4. the first day ────────────────────────────────────────────────────────
def _first_day(args: dict, employee: str, full_name: str, company: str, report: dict) -> None:
	"""Raise safety training and camp orientation. Failure here is never fatal.

	A task that could not be raised — because the dispatch doctypes are not on
	this site, or because the housing unit named does not exist — must not undo an
	onboarding that otherwise worked. It is reported in `skipped` and somebody
	raises it by hand.
	"""
	if not compat.doctype_exists("Farm Task"):
		report["skipped"].append(
			{"step": "first-day tasks", "reason": "this site has no Farm Task doctype; run bench migrate"}
		)
		return

	unit = as_str(args, "housing_unit")
	for spec in FIRST_DAY_TASKS:
		payload = {
			"task_name": spec["task_name"].format(name=full_name),
			"task_type": spec["task_type"],
			"evidence_required": dict(spec["evidence_required"]),
			"company": company,
			"urgency": "High",
			"notes": spec["notes"],
		}
		if spec["key"] == "camp_orientation" and unit and compat.doctype_exists("Housing Unit"):
			payload["location_doctype"] = "Housing Unit"
			payload["location"] = unit
		try:
			result = dispatch.create_farm_task(payload)
		except Exception as exc:
			report["skipped"].append(
				{"step": f"task:{spec['key']}", "reason": f"{type(exc).__name__}: {exc}"}
			)
			continue
		report["tasks"].append({"key": spec["key"], "task": result.data.get("name")})
		report["steps"].append({"step": f"task:{spec['key']}", "action": "created"})

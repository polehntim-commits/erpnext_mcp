# SPDX-License-Identifier: MIT
"""The farm today: department → crew → worker, and the shift nobody closed.

v0.131.0. THREE CALLS BEHIND ONE SCREEN. The handset has been able to ask about
one shift since v0.19.3 — `get_shift` answers what happened on a shift,
`get_shift_production` answers who picked what on it, `get_shift_crew_timeline`
answers what happened to each person on it — and every one of those takes a
docname the caller must already have. There has never been a call that answers
the question a foreman opens the app with at six in the morning, which is not
about a shift at all: WHO IS OUT THERE RIGHT NOW, AND UNDER WHOM.

`list_shifts` is the closest thing and it is a register listing: a flat page of
rows ordered by start time, every entity the caller can reach, no crew on them
and no structure over them. A screen built on it has to fetch the register, then
fetch each shift, then fetch each shift's production, and then invent the
grouping itself — which is three round trips per crew on a radio that is the
worst part of this system, and a grouping that is the client's opinion rather
than the server's answer.

────────────────────────────────────────────────────────────────────────────
THE HIERARCHY IS DEPARTMENT → CREW → WORKER, AND THE MIDDLE ONE IS A SHIFT
────────────────────────────────────────────────────────────────────────────

A CREW IS NOT A RECORD ON THIS SITE. There is no Crew doctype and this release
does not add one, because the thing a foreman means by "my crew" already exists
and is called an open Farm Shift: it has the people on it, the person answerable
for them, when they started and where they are. Adding a register beside it would
put two answers on the farm to "who is Ana working under today" and guarantee
they disagree by the second week.

A DEPARTMENT IS NOT ON THE SHIFT EITHER, and it is not added here. `Farm Shift`
carries a foreman and a company and no department column, so the department of a
crew is READ THROUGH ITS FOREMAN — `Employee.department` of the person answerable
for it. That is a derivation and it is stated rather than hidden, because it has
a consequence a caller can see: move a foreman between departments and yesterday's
crews move with them. The alternative — stamping a department onto the shift at
`start_shift` — is a schema change to five tools and a migration of every shift
already on the register, for a column that would then have to be kept in step
with the foreman's own by hand. This reads the answer from the one place it is
maintained.

────────────────────────────────────────────────────────────────────────────
WHAT SCOPES THE READ, WHICH IS TWO THINGS AND NOT ONE
────────────────────────────────────────────────────────────────────────────

ENTITY FIRST, THE WAY EVERYTHING ELSE ON THIS APP IS SCOPED. `roles.companies_for`
on the MCP path and `guard.require_scope` on the handset, and neither is weakened
here. A Company the caller cannot reach contributes nothing and is not named.

DEPARTMENT SECOND, AND IT IS NEW. `require_company_scope` is the whole of the
scoping every other shift tool applies, which is correct for those tools and is
not enough for this one: a farm with one Company and four departments has four
foremen who can all reach every shift on the site, and a screen that opens on
"the farm today" would show each of them the other three's crews. That is not a
leak of anything secret — they are colleagues — but it is the wrong screen, and
on a forty-crew operation it is an unusable one.

SO THE RULE IS: A FOREMAN SEES THEIR OWN DEPARTMENT AND ITS CHILDREN. A manager
sees every department in the entities they may reach. `_visible_departments`
draws that line and `MANAGER_ROLES` is where it is drawn — Farm Manager, HR
Manager, HR User and System Manager see the operation; Foreman and Crew Leader
see their own part of it.

THE FOREMAN'S DEPARTMENT COMES OFF THEIR OWN EMPLOYEE RECORD, so an account with
no linked Employee, or an Employee with no department, has no department scope to
compute. THAT IS REPORTED AND NOT GUESSED. `scope.reason` says which of the two
it was and the overview comes back empty rather than either falling open to the
whole farm or answering with a bare empty list that reads on a phone as "nobody
is working today". A blank screen with no sentence on it is the failure this
paragraph exists to prevent.

DEPARTMENTS ARE A TREE AND THE WALK IS DOWNWARD ONLY. `Department.parent_department`
is walked from the caller's own department to its descendants, so a foreman over
Harvest sees Harvest, Harvest — Night and Harvest — Packhouse. It is walked in
Python over one query per level rather than with `lft`/`rgt`, because the nested-set
columns are maintained by Frappe HR's own controller and a site whose tree was
edited outside it has stale bounds — the parent links are the data, the bounds are
a cache of it, and this reads the data. `DEPTH_CAP` stops a cycle that a hand-edited
tree can contain.

────────────────────────────────────────────────────────────────────────────
`get_worker_detail` CARRIES A NARROW SHAPE ON PURPOSE
────────────────────────────────────────────────────────────────────────────

IT IS NOT `get_employee` WITH A SCOPE CHECK IN FRONT. The Employee register holds
a date of birth, a home address, a bank account and a salary, and the I-9 register
beside it holds a social security number and a passport number. A foreman running
a crew needs none of that and this returns none of it: `WORKER_FIELDS` is the
whole of what leaves the building, and it is a tuple rather than a `get_doc` so a
column added to Employee next year does not silently join the payload.

THE COMPLIANCE BLOCK REPORTS STANDING AND NEVER EVIDENCE. Training comes back as
`current` / `due_soon` / `expired` / `missing` per curriculum through the same
`_cell` the compliance matrix uses, so the two cannot disagree about what expired
means. The I-9 comes back as ITS STATUS WORD AND NOTHING ELSE — not the documents,
not the numbers on them, not the expiry of a passport. A foreman needs to know
that somebody's work authorisation needs re-verifying; they do not need, and this
does not carry, the document it would be re-verified against.

CERTIFICATIONS ARE MATCHED BY NAME AND IT IS SAID SO. `Certification.holder` is a
Data column rather than a Link — a certificate may be held by a person, a related
party or the Company itself, which is why `evidence._resolve_holder` exists — so a
worker's certificates are found by matching that text against their docname and
their employee name. `certifications_matched_by` says which, and a farm that types
holders inconsistently gets a short list rather than a wrong one.

────────────────────────────────────────────────────────────────────────────
`end_stale_shift` AND THE SIGNATURE IT DOES NOT FORGE
────────────────────────────────────────────────────────────────────────────

THE FOURTH ENDING, AND THE FIRST ONE THAT ADMITS NOBODY WAS THERE TO SIGN IT.
`end_shift` closes a shift with a supervisor's signature and writes the crew's
Attendance; `cancel_shift` says the day was not worked and writes none. Both
assume somebody is standing there at the moment of the ending. A runaway crew is
the case where nobody was: the shift started at 06:00 on Tuesday, the phone died
or the screen was never reopened, and it is now Thursday morning with SHIFT-2026-0114
still Active, still being walked by the weather sweep, still reported by
`list_shifts` as work in progress, and still blocking every one of its crew from
being rostered onto anything else — `_refuse_a_second_open_shift` is what makes
that last one bite, and it is why a forgotten clock-out is not a cosmetic problem.

THE SIGNATURE IS OPTIONAL HERE AND ITS ABSENCE IS RECORDED RATHER THAN HIDDEN.
That is the whole of what this tool adds and it is worth being exact about.
`end_shift` is right to refuse without one: FSMA §112.161(b) asks for a review
that is dated AND SIGNED, and a tool that shipped a signature-free close of an
ordinary shift would turn the attestation into a checkbox within a week. But the
supervisor who could sign for Tuesday is not available on Thursday, and the choice
on Thursday is not between a signed close and an unsigned one — it is between an
unsigned close and a shift that stays open for ever. So this closes it, leaves
`supervisor_review_signature` EMPTY, and returns `supervisor_review_owed: true`
with the sentence saying so. A caller who DOES have a signature passes it and gets
a close indistinguishable from `end_shift`'s.

IT IS FENCED BY A STALENESS THRESHOLD SO IT CANNOT BECOME THE ORDINARY CLOSE.
`STALE_AFTER_HOURS` is sixteen, which is past the end of any real shift and short
of a day, and a shift younger than that is REFUSED BY NAME with `end_shift` named
as the tool for it. `stale_after_hours` lowers it deliberately for the operation
that runs eight-hour shifts and wants twelve; it is an argument rather than a
setting because the number is a judgement about one shift and not a property of
the farm.

`end_datetime` IS OPTIONAL AND ITS DEFAULT IS A WORKING DAY, NOT `now`. v0.140.0.
Until then it was required outright, and the argument for that was sound as far
as it went: a crew that stopped at 14:00 on Tuesday and is clocked out on
Thursday morning would be credited with forty hours by a default of `now`, and
every one of them would reach an Attendance row and a pay cheque. But that
argument is against a default that GROWS WITH THE DELAY — and how long nobody
noticed is precisely the quantity this tool selects its subjects on, so the
worse the neglect the wronger the number. `ASSUMED_SHIFT_HOURS` is eight and
does not grow at all: an ordinary day's work, clamped so it can never reach past
this instant. A crew that worked six is overpaid by two rather than by
thirty-four; a crew that worked ten is underpaid by two, which is the direction
somebody notices and therefore the direction that gets corrected. The answer
carries `end_datetime_assumed: true` and the shift's own notes say so, because
an assumption that reads like a measurement is the only version of this that
would be dishonest.

THE CREW IS RELEASED AND THE ROWS ARE KEPT. Every crew member still carrying no
`left_at` gets the shift's end time, which is what "removes the workers" means
operationally and is the same storage `remove_worker_from_shift` uses — the row
IS the record that they were on the shift at all, and deleting it would destroy
the evidence a wage claim turns on. Attendance is written through the same bridge
`end_shift` uses, so the crew is paid for the span the caller attested to. A close
that wrote no Attendance would be wrong in the employer's favour on a day the crew
actually worked, which is the failure mode this app names in `end_shift` and does
not get to commit here.
"""

from __future__ import annotations

import frappe

from .. import compat, shifts, training
from ..args import as_bool, as_int, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import employee as employee_tool
from . import training as training_tool

DOCTYPE = shifts.DOCTYPE
EMPLOYEE = "Employee"
DEPARTMENT = "Department"
ASSIGNMENT = "Farm Task Assignment"
BUCKET_LOG = "Bucket Log Entry"
I9_FORM = "I-9 Form"
CERTIFICATION = "Certification"

#: Who sees the whole operation rather than one department. `HR_ROLES` itself
#: plus nothing — read off `employee_tool` rather than re-listed, so the set that
#: decides "this account is the office" here and the set that decides it for the
#: personnel register cannot drift apart. Foreman and Crew Leader are the two
#: roles in `SHIFT_ROLES` and not in this one, which is exactly the distinction
#: this module needs and the reason it is expressed as a difference.
MANAGER_ROLES = frozenset(employee_tool.HR_ROLES)

#: Who may call the three tools in this module: `SHIFT_ROLES` MINUS Crew Leader,
#: and the subtraction is deliberate rather than an oversight.
#:
#: THE TWO TRANSPORTS HAVE TO AGREE AND `require_shift_role` WOULD NOT LET THEM.
#: The mobile wrappers carry `guard.require_dispatch_role`, which is
#: {Foreman, Farm Manager}; `SHIFT_ROLES` adds Crew Leader on top of the office
#: roles. Gating the tool bodies on `SHIFT_ROLES` would therefore REFUSE a Crew
#: Leader on the handset and ALLOW them over MCP — which is the exact shape
#: `SHIFT_ROLES`' own comment says it exists to prevent ("a handset showing a
#: button the server refused"), running backwards. It matters most on
#: `end_stale_shift`: that is a write which deliberately skips the FSMA
#: §112.161(b) supervisor signature, and the more permissive path for it should
#: not be the one an operator is least likely to be looking at.
#:
#: A CREW LEADER RUNS A CREW AND DOES NOT SURVEY THE DEPARTMENT, which is the
#: substantive reason and not just a consistency one. `SHIFT_ROLES` exists so the
#: person standing on the block can form a roster, log a water break and sign a
#: shift off — their own crew, which they are already on. These three answer
#: across crews: every crew in a department, any worker's standing, and the close
#: of a shift somebody ELSE is answerable for.
#:
#: THE OFFICE ROLES ARE STILL WIDER THAN THE HANDSET'S AND THAT IS THE HOUSE
#: PATTERN. An HR Manager reaches these over MCP and would meet the dispatch gate
#: on a phone, exactly as they do on every other dispatch-gated route in this app.
#: The Crew Leader case is the one worth closing, because Crew Leader is in
#: `SHIFT_ROLES` BECAUSE OF the handset.
CREW_VIEW_ROLES = tuple(role for role in employee_tool.SHIFT_ROLES if role != "Crew Leader")

#: How deep the department tree is walked before the walk is abandoned. Frappe
#: HR's own tree is rarely three levels; twelve is far past any real org chart
#: and is here to terminate a CYCLE, which a `parent_department` edited by hand
#: or by a bad import can contain and which `lft`/`rgt` would not survive either.
DEPTH_CAP = 12

#: The hours a shift must have been open before `end_stale_shift` will touch it.
#: Sixteen: past the end of any shift a person actually works, and short of the
#: twenty-four that would let a genuinely forgotten night crew sit another day.
STALE_AFTER_HOURS = 16

#: The span an administrative close credits when the caller does not state one.
#:
#: EIGHT, AND THE NUMBER IS THE WHOLE OF THE ARGUMENT. A default of `now` was
#: refused outright until v0.140.0 and was right to be: a crew that stopped at
#: 14:00 on Tuesday, clocked out on Thursday morning, would be credited with
#: forty hours, and every one of them would reach an Attendance row and a pay
#: cheque. That failure is UNBOUNDED — it grows with how long nobody noticed,
#: which is exactly the quantity a stale shift is selected on.
#:
#: Eight hours is bounded by construction and does not grow with the delay. It
#: is an ordinary day's work, so a crew that worked an ordinary day is paid
#: correctly by it; a crew that worked six is overpaid by two rather than by
#: thirty-four, and a crew that worked ten is UNDERPAID by two — which is the
#: direction that matters, because it is the one somebody complains about and
#: therefore the one that gets corrected. `end_datetime` is still the right
#: answer and is still what the caller should send; this is what happens when
#: nobody knows, and the answer says so in `end_datetime_assumed`.
ASSUMED_SHIFT_HOURS = 8

#: The most crews one overview will describe, and the most workers under each.
#: A forty-crew farm is a real farm; four hundred is a runaway query, and the
#: truncation is REPORTED rather than silent — see [[no silent caps]] in the
#: module docstring's sibling tools.
CREW_CAP = 200
WORKER_CAP = 500

#: EVERYTHING THIS MODULE WILL SAY ABOUT A PERSON, and the reason it is a tuple
#: rather than a `get_doc`. The Employee register carries a date of birth, a
#: home address, a personal email, a bank account and a salary; a crew screen
#: needs a name, a job title and a department, and the difference between those
#: two lists is the entire PII argument this release makes. A column added to
#: Employee next year joins the register and does not join this payload.
WORKER_FIELDS = (
	"name",
	"employee_name",
	"designation",
	"department",
	"company",
	"status",
	"date_of_joining",
	# THE ONE ENTRY HERE SOMEBODY WILL ASK ABOUT, so it is argued rather than
	# assumed. A photograph is PII by most definitions. It earns its place
	# because telling people apart IS the crew screen's job — a foreman with
	# forty seasonal workers matches a face to a name, and two people called Ana
	# Ramos is a real situation this app already refuses to guess about
	# elsewhere. It is the Employee register's own `image`, which the badge and
	# the ID card already carry to the same phone.
	"image",
)

#: The assignment states that mean somebody is on a job RIGHT NOW. `Claimed` is
#: included and `Paused` is not: a claimed task is the one they are walking to
#: and is what a crew screen should show against their name, where a paused one
#: is a job they stepped away from and the honest answer to "what is Ana doing"
#: is then nothing. `Completed`, `Rejected` and `Merged` are endings.
ACTIVE_ASSIGNMENT_STATES = ("Claimed", "In-Progress")

#: The bucket verdicts that count as picked. `_compute_shift_production` reads
#: the same two words off the same column and this matches it deliberately — a
#: crew screen whose bucket count disagreed with the production board would be
#: two answers to one question about somebody's piece-rate day.
ACCEPTED_VERDICTS = ("Accepted", "Linked")


def _require_crew_role() -> str:
	"""The principal this call is attributed to, once it may survey a department.

	`employee_tool._require_one_of` with `CREW_VIEW_ROLES` — the same identity
	resolution, the same refusal shape and the same company scope applied by the
	caller afterwards as every other gate in that module. Written as a call into
	the shared body rather than as a fourth copy of it, so a change to how this
	app resolves a principal reaches this module too.
	"""
	return employee_tool._require_one_of(
		CREW_VIEW_ROLES,
		"a crew read across a department",
		"survey a department's crews or close another crew's shift",
	)


def _require() -> None:
	compat.require_doctype(
		DOCTYPE,
		"It ships with erpnext_mcp — run `bench migrate` on this site.",
	)


def _readable_companies(actor: str) -> list:
	"""The entities this principal may reach, Frappe's rule, not the phone's.

	`roles.companies_for` returns [] for an UNRESTRICTED account, which is what
	every Desk surface on the site already means by it, and this module reads
	that as "all of them" the way `tools/shifts.py` does. `api/guard.py` inverts
	the default for the handset and says why at length; the inversion happens in
	the wrapper, before this is reached, so both transports end up scoped and
	neither has to know how the other did it.
	"""
	from .. import roles as role_lib

	return [name for name in (role_lib.companies_for(actor) or []) if name]


def _employee_of(actor: str) -> dict:
	"""The Employee record behind a login, or {} where there is not one.

	NOT AN ERROR ON ITS OWN. An operator's MCP System User has no Employee record
	and never will, and it is precisely the account that should see the whole
	farm — so "no linked Employee" is a fact the caller below decides what to do
	with rather than a refusal thrown from here.
	"""
	if not compat.doctype_exists(EMPLOYEE) or not compat.has_field(EMPLOYEE, "user_id"):
		return {}
	row = frappe.db.get_value(
		EMPLOYEE,
		{"user_id": str(actor or "").lower()},
		compat.existing_fields(EMPLOYEE, ("name", "employee_name", "department", "company")),
		as_dict=True,
	)
	return dict(row or {})


def _descendants_of(department: str) -> list:
	"""One department and everything under it, walked down `parent_department`.

	ONE QUERY PER LEVEL, not one per node. A tree of forty departments five deep
	is five statements; the per-node walk is forty, and this runs on the screen a
	foreman opens first thing in the morning.

	THE PARENT LINKS ARE THE DATA AND `lft`/`rgt` ARE A CACHE OF THEM. Frappe
	HR's NestedSet controller maintains the bounds on its own writes, so a site
	whose tree was touched by an import, a patch or a direct SQL correction can
	carry bounds that no longer describe the links. Reading the links cannot be
	stale in that way, and the cost of it is one small query per level.
	"""
	if not compat.doctype_exists(DEPARTMENT) or not compat.has_field(DEPARTMENT, "parent_department"):
		return [department] if department else []

	found = [department]
	frontier = [department]
	for _ in range(DEPTH_CAP):
		if not frontier:
			break
		children = (
			frappe.db.get_all(
				DEPARTMENT,
				filters={"parent_department": ("in", frontier)},
				pluck="name",
				limit=1000,
			)
			or []
		)
		# A cycle in a hand-edited tree returns a node already seen; dropping it
		# here is what makes DEPTH_CAP a backstop rather than the terminator.
		frontier = [str(name) for name in children if str(name) not in found]
		found.extend(frontier)
	return found


def _visible_departments(actor: str) -> dict:
	"""Which departments this principal may see, and — always — why.

	THE `reason` IS NOT DECORATION. Three of the four outcomes here produce an
	EMPTY overview, and an empty overview on a phone is indistinguishable from a
	quiet morning unless something says which it was. "You hold no role that
	scopes a department", "your login has no Employee record" and "your Employee
	record has no department" are three different problems with three different
	people who can fix them, and the screen cannot say so unless this does.

	AN UNRESTRICTED SET IS `None`, NOT `[]`. The empty list is a real answer here
	— it means this caller sees no department at all — so the two cannot share a
	spelling. Every reader below tests `is None` first.
	"""
	held = set(frappe.get_roles(actor) or [])
	if not held:
		from .. import roles as role_lib

		held = set(role_lib.all_roles_of(actor) or [])

	if held & MANAGER_ROLES:
		return {
			"kind": "entity",
			"departments": None,
			"actor_employee": None,
			"actor_department": None,
			"reason": (
				"This account holds a management role, so its scope is the ENTITIES it may "
				"reach rather than one department — every department in those companies is "
				"below."
			),
		}

	linked = _employee_of(actor)
	if not linked.get("name"):
		return {
			"kind": "department",
			"departments": [],
			"actor_employee": None,
			"actor_department": None,
			"reason": (
				f"{actor} is not linked to an Employee record, so there is no department to "
				"scope this read to and none was assumed. A crew screen scoped to a foreman "
				"reads that foreman's own department off their Employee record. "
				"link_employee_to_user connects the two; the account keeps its role either "
				"way. NOTHING WAS WITHHELD FOR A PERMISSION REASON — this is a missing link, "
				"not a refusal."
			),
		}

	department = str(linked.get("department") or "")
	if not department:
		return {
			"kind": "department",
			"departments": [],
			"actor_employee": linked.get("name"),
			"actor_department": None,
			"reason": (
				f"{linked.get('employee_name') or linked.get('name')} has no department on "
				"their Employee record, so there is no department to scope this read to. It "
				"was NOT widened to the whole farm — an account that should see every crew "
				"holds a management role, and inferring one from a blank column is how a "
				"scoped read stops being scoped. update_employee sets the department."
			),
		}

	visible = _descendants_of(department)
	return {
		"kind": "department",
		"departments": sorted(visible),
		"actor_employee": linked.get("name"),
		"actor_department": department,
		"reason": (
			f"Scoped to {department} and the {len(visible) - 1} department(s) under it, read "
			f"off {linked.get('employee_name') or linked.get('name')}'s own Employee record."
		),
	}


def _department_names(names) -> dict:
	"""docname → the department's display name, in one query for the whole set."""
	wanted = sorted({str(name) for name in names if name})
	if not wanted or not compat.doctype_exists(DEPARTMENT):
		return {}
	rows = (
		frappe.db.get_all(
			DEPARTMENT,
			filters={"name": ("in", wanted)},
			fields=compat.existing_fields(DEPARTMENT, ("name", "department_name", "company")),
			limit=len(wanted),
		)
		or []
	)
	return {str(dict(row)["name"]): dict(row) for row in rows}


def _departments_of(employees) -> dict:
	"""employee docname → their department, in ONE query for the whole roster.

	One statement rather than one per foreman, for the reason `shifts.minor_flags`
	gives about a thirty-person crew: this is called with every foreman on the
	farm and the answer is a single column.
	"""
	wanted = sorted({str(name) for name in employees if name})
	if not wanted or not compat.doctype_exists(EMPLOYEE):
		return {}
	rows = (
		frappe.db.get_all(
			EMPLOYEE,
			filters={"name": ("in", wanted)},
			fields=compat.existing_fields(EMPLOYEE, ("name", "employee_name", "department", "designation")),
			limit=len(wanted),
		)
		or []
	)
	return {str(dict(row)["name"]): dict(row) for row in rows}


def _current_tasks(employees, shift_names) -> dict:
	"""employee → the job they are on right now, in one query for the whole farm.

	NARROWED TO THE SHIFTS BEING DESCRIBED where the column exists. An assignment
	carries `farm_shift` since v0.64.0, and reading only the assignments tied to
	the crews on screen is what stops a worker's task from LAST Tuesday appearing
	against their name today. A site whose assignments predate that column falls
	back to the state filter alone, which over-reports rather than under-reports
	and is said so in the payload.
	"""
	wanted = sorted({str(name) for name in employees if name})
	if not wanted or not compat.doctype_exists(ASSIGNMENT):
		return {}

	filters: dict = {
		"assigned_to": ("in", wanted),
		"state": ("in", list(ACTIVE_ASSIGNMENT_STATES)),
	}
	scoped_to_shift = compat.has_field(ASSIGNMENT, "farm_shift") and bool(shift_names)
	if scoped_to_shift:
		filters["farm_shift"] = ("in", sorted({str(name) for name in shift_names if name}))

	rows = (
		frappe.db.get_all(
			ASSIGNMENT,
			filters=filters,
			fields=compat.existing_fields(
				ASSIGNMENT,
				(
					"name",
					"task",
					"task_name",
					"assigned_to",
					"state",
					"claimed_at",
					"started_at",
					"farm_shift",
				),
			),
			order_by="modified desc",
			limit=WORKER_CAP * 2,
		)
		or []
	)

	out: dict = {}
	for raw in rows:
		row = dict(raw)
		worker = str(row.get("assigned_to") or "")
		# First wins: the rows arrive newest-modified first, so the one kept is
		# the job most recently touched, which is the one somebody is on.
		if worker and worker not in out:
			out[worker] = {
				"assignment": row.get("name"),
				"task": row.get("task"),
				"task_name": row.get("task_name") or row.get("task"),
				"state": row.get("state"),
				"claimed_at": str(row.get("claimed_at") or "") or None,
				"started_at": str(row.get("started_at") or "") or None,
				"farm_shift": row.get("farm_shift") or None,
				"scoped_to_shift": scoped_to_shift,
			}
	return out


def _bucket_counts(shift_names) -> dict:
	"""(shift, employee) → accepted bucket count, in one query for every crew.

	THE SAME COLUMN AND THE SAME TWO VERDICTS `_compute_shift_production` READS.
	A crew screen that counted buckets differently from the production board
	would be two answers to one question about a piece-rate day, and the one on
	the phone is the one the picker would argue from.

	KEYED ON THE PAIR AND NOT ON THE WORKER. Somebody rostered onto two crews in
	one day — a picker moved to the packhouse at noon — has a count under each,
	and collapsing them onto the person would put the morning's buckets against
	the afternoon's crew.
	"""
	names = sorted({str(name) for name in shift_names if name})
	if not names or not compat.doctype_exists(BUCKET_LOG):
		return {}

	shift_field = compat.first_field(BUCKET_LOG, "shift", "farm_shift")
	picker_field = compat.first_field(BUCKET_LOG, "employee", "picker_id", "picker", "worker")
	if not shift_field or not picker_field:
		return {}
	verdict_field = compat.first_field(BUCKET_LOG, "verdict", "status")

	fields = [shift_field, picker_field]
	if verdict_field:
		fields.append(verdict_field)

	counts: dict = {}
	for raw in (
		frappe.db.get_all(
			BUCKET_LOG, filters={shift_field: ("in", names)}, fields=fields, limit_page_length=0
		)
		or []
	):
		row = dict(raw)
		picker = str(row.get(picker_field) or "")
		if not picker:
			continue
		verdict = str(row.get(verdict_field) or "Accepted") if verdict_field else "Accepted"
		if verdict not in ACCEPTED_VERDICTS:
			continue
		key = (str(row.get(shift_field) or ""), picker)
		counts[key] = counts.get(key, 0) + 1
	return counts


def _hours_open(row: dict, now: str) -> float | None:
	return shifts.hours_between(str(row.get("start_datetime") or ""), now)


# ── the read both crew tools are built on ───────────────────────────────────
def _open_shifts_visible_to(actor: str, args: dict, cap: int = CREW_CAP) -> dict:
	"""Every open shift this caller may see, with the department of each.

	SHARED BY `get_crew_overview` AND `list_active_shifts` BECAUSE THE TWO MUST
	NOT BE ABLE TO DISAGREE. They answer the same question at two weights — one
	nested with every worker on every crew, one flat and light enough for a
	summary bar — and a farm where the list showed four crews and the overview
	showed three would be a farm where nobody trusts either. So "which open
	shifts, in which entities, in which departments, and which of them are
	stale" is decided ONCE, here, and both callers shape what comes back rather
	than re-deriving it.

	`withheld` IS COUNTED RATHER THAN DROPPED SILENTLY. A caller who can see two
	of six crews should be told there are four they cannot, because an
	unexplained short list reads as a quiet morning.
	"""
	company = resolve_company(as_str(args, "company"), required=False)
	allowed = _readable_companies(actor)
	if company:
		employee_tool.require_company_scope(actor, company)
		companies = [company]
	else:
		companies = list(allowed)

	scope = _visible_departments(actor)
	visible = scope["departments"]
	now = frappe.utils.now()

	filters: dict = {"end_datetime": ("is", "not set")}
	if compat.has_field(DOCTYPE, "cancelled"):
		filters["cancelled"] = 0
	if companies:
		filters["company"] = ("in", companies)

	rows = shifts.rows(filters, limit=cap + 1, order_by="start_datetime asc")
	truncated = len(rows) > cap
	rows = rows[:cap]

	# The department of a crew is its foreman's — resolved for every foreman on
	# the farm in ONE query rather than one per crew.
	foremen = _departments_of([row.get("foreman") for row in rows])

	# AN EXPLICIT `department` NARROWS AND CANNOT WIDEN. It is intersected with
	# what the caller may already see, so naming somebody else's department
	# returns nothing rather than reaching into it.
	wanted = str(as_str(args, "department") or "").strip()

	kept = []
	withheld = 0
	for row in rows:
		department = str((foremen.get(str(row.get("foreman") or "")) or {}).get("department") or "")
		if visible is not None and department not in set(visible):
			withheld += 1
			continue
		if wanted and department != wanted:
			continue
		kept.append((row, department))

	return {
		"actor": actor,
		"companies": companies,
		"scope": scope,
		"now": now,
		"kept": kept,
		"foremen": foremen,
		"withheld": withheld,
		"truncated": truncated,
		"department_filter": wanted or None,
	}


# ── 1. get_crew_overview ────────────────────────────────────────────────────
def get_crew_overview(args: dict) -> ToolResult:
	"""Who is out on the farm right now, by department, by crew, by name."""
	_require()
	actor = _require_crew_role()

	found = _open_shifts_visible_to(actor, args)
	companies = found["companies"]
	scope = found["scope"]
	visible = scope["departments"]
	now = found["now"]
	kept = found["kept"]
	foremen = found["foremen"]
	withheld = found["withheld"]
	truncated = found["truncated"]

	shift_names = [str(row.get("name") or "") for row, _ in kept]
	crews_by_department: dict = {}
	all_workers: list = []
	crew_rows_by_shift: dict = {}
	for row, _department in kept:
		crew = shifts.crew_of(str(row.get("name") or ""))[:WORKER_CAP]
		crew_rows_by_shift[str(row.get("name") or "")] = crew
		all_workers.extend(str(entry.get("employee") or "") for entry in crew)

	tasks = _current_tasks(all_workers, shift_names)
	buckets = _bucket_counts(shift_names)

	total_workers = 0
	for row, department in kept:
		name = str(row.get("name") or "")
		crew = crew_rows_by_shift.get(name) or []
		workers = []
		for entry in crew:
			worker = str(entry.get("employee") or "")
			workers.append(
				{
					"employee": worker or None,
					"employee_name": entry.get("employee_name") or worker,
					"role": entry.get("role") or "Worker",
					"joined_at": str(entry.get("joined_at") or "") or None,
					"left_at": str(entry.get("left_at") or "") or None,
					"still_on_shift": not entry.get("left_at"),
					"current_task": tasks.get(worker),
					"bucket_count": buckets.get((name, worker), 0),
				}
			)
		still_on = len([entry for entry in workers if entry["still_on_shift"]])
		total_workers += still_on

		crews_by_department.setdefault(department, []).append(
			{
				"shift": name,
				"shift_type": row.get("shift_type") or None,
				"crew_leader": row.get("foreman") or None,
				"crew_leader_name": row.get("foreman_name")
				or (foremen.get(str(row.get("foreman") or "")) or {}).get("employee_name")
				or row.get("foreman"),
				"company": row.get("company") or None,
				"location": row.get("location") or None,
				"farm_location_gps": row.get("farm_location_gps") or None,
				"start_datetime": str(row.get("start_datetime") or "") or None,
				"hours_open": _hours_open(row, now),
				"worker_count": len(workers),
				"still_on_shift": still_on,
				"buckets_accepted": sum(entry["bucket_count"] for entry in workers),
				"stale": _is_stale(row, now, STALE_AFTER_HOURS),
				"workers": workers,
			}
		)

	labels = _department_names(crews_by_department.keys())
	departments = []
	for department in sorted(crews_by_department, key=lambda value: (value == "", value)):
		crews = crews_by_department[department]
		label = labels.get(department) or {}
		departments.append(
			{
				"department": department or None,
				"department_name": label.get("department_name") or department or "No department",
				"company": label.get("company") or None,
				"crew_count": len(crews),
				"worker_count": sum(crew["still_on_shift"] for crew in crews),
				"stale_crew_count": len([crew for crew in crews if crew["stale"]]),
				"crews": crews,
			}
		)

	stale = [crew["shift"] for entry in departments for crew in entry["crews"] if crew["stale"]]
	data = {
		"as_of": now,
		"actor": actor,
		"companies": companies,
		"scope": scope,
		"departments": departments,
		"department_count": len(departments),
		"crew_count": sum(entry["crew_count"] for entry in departments),
		"worker_count": total_workers,
		"stale_shifts": sorted(stale),
		"stale_after_hours": STALE_AFTER_HOURS,
	}

	if visible is not None and not visible:
		data["note"] = scope["reason"]
	elif not departments:
		data["note"] = (
			"No crew is open on this farm right now. Every Farm Shift in the entities this "
			"account may reach is closed or cancelled — that is a quiet morning rather than a "
			"scoping problem, and start_shift is what opens one."
		)
	if withheld:
		data["withheld_note"] = (
			f"{withheld} open crew(s) belong to a department outside this account's scope and "
			f"are not above. {scope['reason']}"
		)
	if stale:
		data["stale_note"] = (
			f"{len(stale)} crew(s) have been open more than {STALE_AFTER_HOURS} hours: "
			f"{', '.join(sorted(stale))}. A shift nobody closed keeps its whole crew off every "
			"other roster — start_shift refuses a second open shift for the same person — and "
			"keeps being fetched for weather it does not need. end_stale_shift closes one."
		)
	if truncated:
		data["truncation_note"] = (
			f"More than {CREW_CAP} crews are open and only the {CREW_CAP} that started earliest "
			"are above. Pass `company` to narrow the read."
		)

	return ToolResult(
		data=data,
		summary=(
			f"{data['crew_count']} crew(s), {total_workers} worker(s) out across "
			f"{len(departments)} department(s)" + (f"; {len(stale)} stale" if stale else "")
		),
	)


def _is_stale(row: dict, now: str, threshold: int) -> bool:
	"""Open for longer than a shift anybody works. Never true of a closed one."""
	if not shifts.is_open(row):
		return False
	hours = shifts.hours_between(str(row.get("start_datetime") or ""), now)
	return hours is not None and hours >= threshold


# ── 1b. list_active_shifts ──────────────────────────────────────────────────
def list_active_shifts(args: dict) -> ToolResult:
	"""Every shift running right now, one row each — the supervisor's glance.

	THE SAME QUESTION AS `get_crew_overview` AT A DIFFERENT WEIGHT, and the
	difference is the whole reason it exists rather than being a flag on that
	one. The overview nests every worker under every crew under every
	department: on a forty-crew farm with fifteen people on each that is six
	hundred worker objects, each carrying a current task and a bucket count, and
	the screen this feeds wants none of it. It wants one line per crew — what is
	running, who has it, where, since when, how many people — small enough to
	poll on a radio that is the worst part of this system.

	THEY CANNOT DISAGREE, WHICH IS WHY BOTH GO THROUGH
	`_open_shifts_visible_to`. Same entity scope, same department scope, same
	staleness threshold, same withheld count, decided once. A farm where the
	list showed four crews and the overview showed three is a farm where nobody
	trusts either, and two functions each deriving "which shifts are open" would
	arrive there the first time one of them grew a filter.

	IT IS NOT `list_shifts` EITHER, AND THAT IS WORTH BEING EXACT ABOUT, because
	a catalogue with two answers to one question is the thing this app keeps
	arguing against. `list_shifts` is the REGISTER: a period, a foreman, a shift
	type, open or closed, ordered by start time, and it is the right tool for
	"what did we run last week". It carries no department, no crew size and no
	department scoping — a Foreman calling it sees every crew in every entity
	they can reach. This carries all three, is open-only by construction, and
	answers "what is running right now, of the crews that are mine to know
	about".

	`stale_only` IS THE RUNAWAY WORKLIST. A shift open past the threshold has
	its whole crew off every other roster — `start_shift` refuses a second open
	shift for the same person — so the shifts nobody closed are not a tidiness
	problem, they are the reason somebody cannot be rostered this morning.
	`end_stale_shift` is what closes one.
	"""
	_require()
	actor = _require_crew_role()

	found = _open_shifts_visible_to(actor, args, cap=min(as_limit(args), CREW_CAP))
	scope = found["scope"]
	now = found["now"]
	foremen = found["foremen"]

	stale_only = as_bool(args, "stale_only", False)
	threshold = as_int(args, "stale_after_hours", None)
	threshold = STALE_AFTER_HOURS if threshold is None else threshold

	labels = _department_names(department for _row, department in found["kept"])

	rows = []
	for row, department in found["kept"]:
		name = str(row.get("name") or "")
		stale = _is_stale(row, now, threshold)
		if stale_only and not stale:
			continue
		# THE CREW IS COUNTED AND NOT CARRIED. One `crew_of` per shift is the
		# same query the overview already makes; what this does not do is put
		# fifteen worker objects on the wire for a row that wants a number.
		crew = shifts.crew_of(name)
		label = labels.get(department) or {}
		rows.append(
			{
				"shift": name,
				"shift_type": row.get("shift_type") or None,
				"crew_leader": row.get("foreman") or None,
				"crew_leader_name": row.get("foreman_name")
				or (foremen.get(str(row.get("foreman") or "")) or {}).get("employee_name")
				or row.get("foreman"),
				"department": department or None,
				"department_name": label.get("department_name") or department or "No department",
				"company": row.get("company") or None,
				"location": row.get("location") or None,
				"farm_location_gps": row.get("farm_location_gps") or None,
				"start_datetime": str(row.get("start_datetime") or "") or None,
				"hours_open": _hours_open(row, now),
				"worker_count": len(crew),
				"still_on_shift": len([entry for entry in crew if not entry.get("left_at")]),
				"stale": stale,
			}
		)

	stale_names = sorted(entry["shift"] for entry in rows if entry["stale"])
	data = {
		"as_of": now,
		"actor": actor,
		"companies": found["companies"],
		"scope": scope,
		"department_filter": found["department_filter"],
		"shifts": rows,
		"count": len(rows),
		"worker_count": sum(entry["still_on_shift"] for entry in rows),
		"stale_shifts": stale_names,
		"stale_after_hours": threshold,
		"stale_only": bool(stale_only),
	}

	visible = scope["departments"]
	if visible is not None and not visible:
		data["note"] = scope["reason"]
	elif not rows and stale_only:
		data["note"] = (
			f"No crew has been open longer than {threshold} hour(s). Nothing is running away — "
			"drop stale_only to see what IS running."
		)
	elif not rows:
		data["note"] = (
			"No crew is open on this farm right now. Every Farm Shift in the entities this "
			"account may reach is closed or cancelled — that is a quiet morning rather than a "
			"scoping problem, and start_shift is what opens one."
		)
	if found["withheld"]:
		data["withheld_note"] = (
			f"{found['withheld']} open crew(s) belong to a department outside this account's "
			f"scope and are not above. {scope['reason']}"
		)
	if stale_names and not stale_only:
		data["stale_note"] = (
			f"{len(stale_names)} crew(s) have been open more than {threshold} hours: "
			f"{', '.join(stale_names)}. A shift nobody closed keeps its whole crew off every "
			"other roster — start_shift refuses a second open shift for the same person. "
			"end_stale_shift closes one; stale_only narrows this read to them."
		)
	if found["truncated"]:
		data["truncation_note"] = (
			f"More than {min(as_limit(args), CREW_CAP)} crews are open and only the ones that "
			"started earliest are above. Pass `company` or `department` to narrow the read."
		)

	return ToolResult(
		data=data,
		summary=(
			f"{len(rows)} crew(s) running, {data['worker_count']} worker(s) out"
			+ (f"; {len(stale_names)} stale" if stale_names else "")
		),
	)


# ── 2. get_worker_detail ────────────────────────────────────────────────────
def get_worker_detail(args: dict) -> ToolResult:
	"""One worker's day and their standing, for the person running their crew."""
	_require()
	actor = _require_crew_role()
	compat.require_doctype(EMPLOYEE, "It comes with the Frappe HR (hrms) app.")

	person = employee_tool.resolve_employee(as_str(args, "employee", required=True))
	row = dict(
		frappe.db.get_value(EMPLOYEE, person, compat.existing_fields(EMPLOYEE, WORKER_FIELDS), as_dict=True)
		or {}
	)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	scope = _visible_departments(actor)
	visible = scope["departments"]
	department = str(row.get("department") or "")
	if visible is not None and department not in set(visible):
		# WORDED AS NOT-VISIBLE RATHER THAN NOT-FOUND, unlike the docname refusals
		# on the handset. The caller already proved a shift role and an entity
		# they share with this person; what they cannot do is read somebody in
		# another department, and telling them which department it is would leak
		# nothing they could not get from `list_departments`.
		raise ToolError(
			f"{row.get('employee_name') or person} is in "
			+ (f"department {department}" if department else "no department")
			+ ", which is outside this account's scope. "
			+ scope["reason"]
			+ " Nothing was read."
		)

	now = frappe.utils.now()
	as_of = now[:10]

	shift = _current_shift_of(person, str(row.get("company") or ""))
	assignments = _todays_assignments(person, as_of)
	compliance = _compliance_of(person, str(row.get("employee_name") or ""), as_of)

	data = {
		"as_of": now,
		"actor": actor,
		"employee": row.get("name"),
		"employee_name": row.get("employee_name"),
		"designation": row.get("designation") or None,
		"department": department or None,
		"company": row.get("company") or None,
		"status": row.get("status") or None,
		"date_of_joining": str(row.get("date_of_joining") or "") or None,
		"photo": row.get("image") or None,
		"scope": scope,
		"current_shift": shift,
		"task_assignments_today": assignments,
		"task_assignment_count": len(assignments),
		"compliance": compliance,
		"fields_note": (
			"THIS IS A CREW SCREEN AND NOT THE EMPLOYEE RECORD. Date of birth, home address, "
			"bank details, wage and every I-9 document field are deliberately absent — the "
			"compliance block below reports STANDING and never evidence. get_employee is the "
			"register read, and it is gated on the personnel roles rather than on a shift role."
		),
	}
	if not shift:
		data["shift_note"] = (
			f"{row.get('employee_name') or person} is not on an open shift. That is the "
			"ordinary state of somebody who has not clocked in yet or whose crew closed — "
			"list_shifts has the register, and get_shift_crew_timeline answers what their day "
			"was on a shift that has ended."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{row.get('employee_name') or person}"
			+ (f" on {shift['shift']}" if shift else " — no open shift")
			+ f"; {len(assignments)} assignment(s) today; "
			f"{compliance['summary']['standing']}"
		),
	)


def _current_shift_of(person: str, company: str) -> dict | None:
	"""The open shift this worker is rostered on, if there is one.

	READ THROUGH THE CREW CHILD TABLE'S PARENT, which is the only way it works on
	both a bench and the standalone double: filtering a child doctype by `parent`
	answers on MariaDB and answers nothing in the harness. So the open shifts are
	fetched first and their crews read through `shifts.crew_of`, which is the
	accessor every other reader in this app uses.
	"""
	filters: dict = {"end_datetime": ("is", "not set")}
	if compat.has_field(DOCTYPE, "cancelled"):
		filters["cancelled"] = 0
	if company:
		filters["company"] = company

	for row in shifts.rows(filters, limit=CREW_CAP, order_by="start_datetime desc"):
		name = str(row.get("name") or "")
		for entry in shifts.crew_of(name):
			if str(entry.get("employee") or "") != person:
				continue
			# THE FOREMAN'S NAME IS RESOLVED RATHER THAN FALLEN BACK TO THEIR
			# DOCNAME. `foreman_name` is a stored copy that `start_shift` stamps,
			# and a shift written by anything else — an import, a fixture, a patch
			# — carries it empty. "Your crew leader is HR-EMP-00001" is the shape
			# of answer this app has printed into a court letter before.
			foreman = str(row.get("foreman") or "")
			leader_name = str(row.get("foreman_name") or "") or str(
				(_departments_of([foreman]).get(foreman) or {}).get("employee_name") or ""
			)
			return {
				"shift": name,
				"shift_type": row.get("shift_type") or None,
				"company": row.get("company") or None,
				"crew_leader": row.get("foreman") or None,
				"crew_leader_name": leader_name or foreman or None,
				"location": row.get("location") or None,
				"farm_location_gps": row.get("farm_location_gps") or None,
				"start_datetime": str(row.get("start_datetime") or "") or None,
				"joined_at": str(entry.get("joined_at") or "") or None,
				"left_at": str(entry.get("left_at") or "") or None,
				"still_on_shift": not entry.get("left_at"),
				"crew_role": entry.get("role") or "Worker",
				"hours_open": shifts.hours_between(str(row.get("start_datetime") or ""), frappe.utils.now()),
				"stale": _is_stale(row, frappe.utils.now(), STALE_AFTER_HOURS),
			}
	return None


def _todays_assignments(person: str, as_of: str) -> list:
	"""Every job filed against this worker today, whatever state it reached.

	NOT ONLY THE OPEN ONES. "What has Ana done today" and "what is Ana doing" are
	different questions and a crew screen asks both — a morning of completed
	tasks is the answer to the first, and dropping them because they are finished
	would make a productive day look like an idle one.
	"""
	if not compat.doctype_exists(ASSIGNMENT):
		return []
	rows = (
		frappe.db.get_all(
			ASSIGNMENT,
			filters={"assigned_to": person, "creation": (">=", f"{as_of} 00:00:00")},
			fields=compat.existing_fields(
				ASSIGNMENT,
				(
					"name",
					"task",
					"task_name",
					"state",
					"claimed_at",
					"started_at",
					"completed_at",
					"actual_duration_minutes",
					"farm_shift",
					"company",
				),
			),
			order_by="creation asc",
			limit=WORKER_CAP,
		)
		or []
	)
	out = []
	for raw in rows:
		row = dict(raw)
		out.append(
			{
				"assignment": row.get("name"),
				"task": row.get("task"),
				"task_name": row.get("task_name") or row.get("task"),
				"state": row.get("state"),
				"claimed_at": str(row.get("claimed_at") or "") or None,
				"started_at": str(row.get("started_at") or "") or None,
				"completed_at": str(row.get("completed_at") or "") or None,
				"actual_duration_minutes": int(row.get("actual_duration_minutes") or 0) or None,
				"farm_shift": row.get("farm_shift") or None,
			}
		)
	return out


def _compliance_of(person: str, employee_name: str, as_of: str) -> dict:
	"""Training currency, I-9 standing and certificates — STANDING, never evidence.

	EVERY ONE OF THE THREE REPORTS A WORD AND NOT A DOCUMENT. The training cells
	come from `training_tool._cell`, which is the same function the compliance
	matrix uses, so "expired" cannot mean one thing on a report and another on a
	phone. The I-9 carries its status Select and NOTHING ELSE — the register
	behind it holds a social security number, a date of birth, a home address and
	a passport number, and a foreman deciding whether somebody may work today
	needs the word `Reverification Needed` and none of the rest.
	"""
	requirements = training_tool._requirements("", "")
	records = _training_records_of(person)
	cells = {}
	for requirement in requirements:
		curriculum = requirement["training_type"]
		cells[curriculum] = training_tool._cell(records.get(curriculum), as_of)

	missing = sorted(name for name, cell in cells.items() if cell["status"] == "missing")
	expired = sorted(name for name, cell in cells.items() if cell["status"] == "expired")
	due_soon = sorted(name for name, cell in cells.items() if cell["status"] == "due_soon")
	current = sorted(name for name, cell in cells.items() if cell["status"] == "current")

	i9 = _i9_of(person)
	certificates = _certificates_of(person, employee_name)

	if expired or missing:
		standing = "non_compliant"
	elif due_soon:
		standing = "attention"
	elif not requirements:
		standing = "no_requirements"
	else:
		standing = "current"

	return {
		"training": {
			"cells": cells,
			"requirement_count": len(requirements),
			"current": current,
			"due_soon": due_soon,
			"expired": expired,
			"missing": missing,
			"basis": (
				"This site has no per-role training requirement table, so the curriculum axis "
				"is every ACTIVE Training Type the operation runs — the same basis "
				"get_training_compliance_report states. It over-reports for somebody whose job "
				"never needed a course, and that is visible rather than hidden."
			),
		},
		"i9": i9,
		"certifications": certificates["rows"],
		"certifications_matched_by": certificates["matched_by"],
		"summary": {
			"standing": standing,
			"training_expired": len(expired),
			"training_missing": len(missing),
			"training_due_soon": len(due_soon),
			"i9_status": i9.get("status"),
			"certifications_expired": len(
				[row for row in certificates["rows"] if row.get("status") == "Expired"]
			),
		},
		"vocabulary_note": (
			"Training statuses are lowercase with underscores — current, due_soon, expired, "
			"missing. The I-9 and Certification statuses are the doctypes' own Select values "
			"and are Title Case. A client matching one vocabulary against the other matches "
			"nothing."
		),
	}


def _training_records_of(person: str) -> dict:
	"""curriculum → this worker's most recent record on it."""
	if not compat.doctype_exists(training.DOCTYPE):
		return {}
	rows = (
		frappe.db.get_all(
			training.DOCTYPE,
			filters={"employee": person},
			fields=compat.existing_fields(
				training.DOCTYPE,
				("name", "training_type", "completed_date", "expires_date", "supervisor_reviewed_by"),
			),
			order_by="completed_date asc",
			limit=WORKER_CAP,
		)
		or []
	)
	# Ascending, so the LAST write per curriculum wins and the newest completion
	# is the one described. A worker who retook a course has two records and only
	# the later one describes their standing.
	out: dict = {}
	for raw in rows:
		row = dict(raw)
		curriculum = str(row.get("training_type") or "")
		if curriculum:
			out[curriculum] = row
	return out


def _i9_of(person: str) -> dict:
	"""The I-9's STATUS WORD and the work-authorisation date. Nothing else.

	NOT THE FORM. Every other column on that register is either a document number
	or a personal identifier, and the reason this function is four fields long is
	that a crew screen has no business carrying any of them. `get_i9_form` reads
	the record and is gated on the personnel roles, which is where that read
	belongs.
	"""
	if not compat.doctype_exists(I9_FORM):
		return {
			"status": None,
			"available": False,
			"note": (
				"This site has no I-9 Form register, so work authorisation standing is not "
				"tracked here. It ships with erpnext_mcp — run `bench migrate`."
			),
		}
	# A DESTROYED ROW IS NOT THIS WORKER'S I-9 AND MUST NOT ANSWER FOR IT.
	# `employee` is not unique on this register and multiple rows per person are
	# the DESIGNED rehire path rather than an edge case: `destroy_i9` sets the
	# status and SAVES the row instead of deleting it, and `create_i9_form`
	# refuses a new form until the existing one is Destroyed. So a rehired worker
	# has two rows by construction, and one of them is the one nothing should
	# read. Five other readers in `tools/i9.py` already exclude it by name; this
	# was the only one that did not.
	#
	# THE FAILURE DIRECTION IS THE BAD ONE, which is why the filter is not merely
	# tidiness: a destroyed row reading `Complete` beside a live one reading
	# `Reverification Needed` would show a worker who may not legally work today
	# as clear, on the screen a foreman uses to decide exactly that.
	#
	# THE ORDER IS STATED RATHER THAN INHERITED. Without it the tie-break is
	# whatever the driver does — `modified desc` on a bench, insertion order in
	# the standalone double — so the two would answer differently and neither
	# would be a decision anybody made.
	rows = (
		frappe.db.get_all(
			I9_FORM,
			filters={"employee": person, "status": ["!=", "Destroyed"]},
			fields=compat.existing_fields(I9_FORM, ("name", "status", "alien_work_authorization_expiry")),
			order_by="modified desc",
			limit=1,
		)
		or []
	)
	row = dict(rows[0]) if rows else {}
	if not row:
		return {
			"status": None,
			"available": True,
			"form": None,
			"note": (
				"No I-9 is on file for this worker. That is the finding an ICE audit opens "
				"with, and it is reported here rather than left blank. create_i9_form starts "
				"one; list_pending_i9_verifications is the worklist."
			),
		}
	return {
		"status": row.get("status"),
		"available": True,
		"form": row.get("name"),
		"work_authorization_expires": str(row.get("alien_work_authorization_expiry") or "") or None,
		"reverification_needed": str(row.get("status") or "") == "Reverification Needed",
		"complete": str(row.get("status") or "") == "Complete",
	}


def _certificates_of(person: str, employee_name: str) -> dict:
	"""This worker's certificates, matched on the register's free-text holder.

	`Certification.holder` IS A `Data` COLUMN AND NOT A LINK, which is why this
	is a match rather than a filter: a certificate may be held by a person, a
	related party or the Company itself, and `evidence._resolve_holder` exists
	precisely because the register cannot say which. So both spellings a person
	appears under are tried, and `matched_by` reports which answered — a farm
	that types holders inconsistently gets a short list rather than a wrong one.
	"""
	if not compat.doctype_exists(CERTIFICATION):
		return {"rows": [], "matched_by": []}

	wanted = [value for value in (person, employee_name) if value]
	if not wanted:
		return {"rows": [], "matched_by": []}

	rows = (
		frappe.db.get_all(
			CERTIFICATION,
			filters={"holder": ("in", wanted)},
			fields=compat.existing_fields(
				CERTIFICATION,
				("name", "cert_name", "cert_type", "status", "holder", "issuing_body", "expiration_date"),
			),
			order_by="expiration_date asc",
			limit=WORKER_CAP,
		)
		or []
	)
	found = [dict(row) for row in rows]
	return {
		"rows": [
			{
				"certification": row.get("name"),
				"cert_name": row.get("cert_name"),
				"cert_type": row.get("cert_type") or None,
				"status": row.get("status"),
				"issuing_body": row.get("issuing_body") or None,
				"expiration_date": str(row.get("expiration_date") or "") or None,
			}
			for row in found
		],
		"matched_by": sorted({str(row.get("holder") or "") for row in found}),
	}


def _assumed_end(start: str, now: str) -> str:
	"""`start` plus a working day, and never later than this instant. v0.140.0.

	THE CLAMP IS NOT DECORATION. `stale_after_hours` is an argument, so a caller
	may lower the fence to four for an operation that runs short shifts — and
	then `start + 8h` is in the FUTURE, which the future check below refuses. A
	default that made the tool refuse itself would be a default nobody could use;
	a default that paid the crew for hours nobody has reached yet would be the
	forty-hour bug in miniature. Clamped, the assumption is "a working day, or as
	much of one as has actually elapsed", which is true in both cases.
	"""
	end = frappe.utils.add_to_date(start, hours=ASSUMED_SHIFT_HOURS, as_string=True, as_datetime=True)
	return now if shifts.to_the_second(str(end)) > shifts.to_the_second(now) else str(end)


# ── 3. end_stale_shift ──────────────────────────────────────────────────────
def end_stale_shift(args: dict) -> ToolResult:
	"""MUTATING. Close a runaway shift nobody clocked out of, and release its crew."""
	_require()
	actor = _require_crew_role()

	row = _resolve_stale_shift(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	if not shifts.is_open(row):
		already = shifts.status_for(row.get("end_datetime"), row.get("cancelled"))
		raise ToolError(
			f"{row['name']} is not open — it is {already} as of {row.get('end_datetime')}. This "
			"tool exists for a shift that was never closed, and a shift that HAS an ending "
			"cannot have another one written over it. Nothing was changed."
		)

	reason = as_str(args, "reason") or as_str(args, "closure_reason")
	if not reason:
		raise ToolError(
			"reason is required. This close carries no contemporaneous supervisor signature "
			"unless one is passed, so the sentence is the only account of why a shift was "
			"ended by somebody who was not standing there — 'crew clocked out at the "
			"packhouse, phone died at 14:00' is a record and a bare timestamp is a gap "
			"somebody will be asked about. THE SHIFT IS STILL OPEN and nothing was changed."
		)

	# NOT `as_int(...) or STALE_AFTER_HOURS`. A caller who passes 0 means zero,
	# and `0 or 16` is 16 — which would answer the deliberately-absurd threshold
	# by quietly substituting the default and CLOSING THE SHIFT the refusal below
	# exists to protect. `None` is the only "was not given".
	threshold = as_int(args, "stale_after_hours", None)
	threshold = STALE_AFTER_HOURS if threshold is None else threshold
	if threshold < 1:
		raise ToolError(
			f"stale_after_hours is {threshold}, which would make every open shift stale — "
			"including the one that started twenty minutes ago. The point of the threshold is "
			"that this tool cannot become the ordinary close. Nothing was changed."
		)

	now = frappe.utils.now()
	start = str(row.get("start_datetime") or "")
	open_for = shifts.hours_between(start, now)
	if open_for is None or open_for < threshold:
		raise ToolError(
			f"{row['name']} started at {row.get('start_datetime')} and has been open "
			+ (f"{open_for} hour(s)" if open_for is not None else "for an unreadable span")
			+ f", which is inside the {threshold}-hour staleness threshold — it is a shift that "
			"is RUNNING, not one that was forgotten. Close it with end_shift, which takes the "
			"supervisor's signature FSMA §112.161(b) asks for and which this tool deliberately "
			"does not require. Pass stale_after_hours to lower the threshold if this really is "
			"a runaway. Nothing was changed."
		)

	# v0.140.0. THE END TIME IS OPTIONAL AND ITS ABSENCE IS ANSWERED WITH A
	# WORKING DAY, NOT WITH `now`. See `ASSUMED_SHIFT_HOURS` for why those are
	# different defaults and not two spellings of one: `now` grows with how long
	# nobody noticed, and how long nobody noticed is the quantity this tool
	# selects its subjects on. A stated `end_datetime` is still the right answer
	# and is still what a caller who knows should send.
	end = as_str(args, "end_datetime") or as_str(args, "ended_at") or as_str(args, "actual_end_time")
	end_assumed = not end
	if end_assumed:
		end = _assumed_end(start, now)

	if shifts.to_the_second(end) < shifts.to_the_second(start):
		raise ToolError(
			f"this call ends the shift at {end} and it started at {start} — it would have "
			"finished before it began, and every crew member's Attendance row would carry a "
			"negative span. Nothing was changed."
		)
	if shifts.to_the_second(end) > shifts.to_the_second(now):
		raise ToolError(
			f"this call ends the shift at {end}, which is in the future — it is {now} on this "
			"site. A close is a statement that work HAS stopped, and an end time nobody has "
			"reached yet would pay the crew for hours they have not worked. Nothing was changed."
		)

	signature = ""
	raw_signature = as_str(args, "supervisor_signature_file_token") or as_str(args, "supervisor_signature")
	if raw_signature:
		from .shifts import file_reference

		signature = file_reference(raw_signature, "supervisor_signature_file_token")

	crew_before = shifts.crew_of(row["name"])
	late = [
		entry
		for entry in crew_before
		if entry.get("left_at") and shifts.to_the_second(entry["left_at"]) > shifts.to_the_second(end)
	]
	if late:
		names = ", ".join(str(entry.get("employee_name") or entry.get("employee")) for entry in late)
		raise ToolError(
			f"{names} is recorded as leaving after {end}, which is when this call ends the "
			"shift. Nobody is on a shift that is over. Correct the departure time with "
			"remove_worker_from_shift, or end the shift later. Nothing was changed."
		)

	doc = frappe.get_doc(DOCTYPE, row["name"])
	released = []
	for entry in doc.crew or []:
		if entry.get("left_at"):
			continue
		entry.left_at = end
		released.append(
			{
				"employee": entry.get("employee"),
				"employee_name": entry.get("employee_name") or entry.get("employee"),
				"joined_at": str(entry.get("joined_at") or "") or None,
				"left_at": end,
				"hours_present": shifts.hours_between(str(entry.get("joined_at") or start), end),
			}
		)

	doc.end_datetime = end
	if signature:
		doc.supervisor_review_signature = signature
		doc.supervisor_review_on = as_str(args, "reviewed_on") or now
	note = (
		f"ADMINISTRATIVE CLOSE by {actor} at {now}: the shift was open "
		+ (f"{open_for} hour(s)" if open_for is not None else "for an unreadable span")
		+ f" and was ended at {end}"
		+ (
			f" — an ASSUMED end time, {ASSUMED_SHIFT_HOURS} hours from the start, because "
			"nobody stated when work actually stopped"
			if end_assumed
			else ""
		)
		+ f". {reason}"
	)
	# EVERY LINE IS KEPT AND NONE OF THEM IS CHOSEN OVER ANOTHER. Until v0.140.0
	# this read `doc.foreman_notes or as_str(args, "foreman_notes")`, which
	# silently DROPPED the caller's sentence on any shift that already carried
	# notes — which is the ordinary case, because a foreman's own notes are what
	# a shift with any history has. An argument the caller was accepted and then
	# discarded is worse than one that was refused.
	lines = [
		part for part in (str(doc.foreman_notes or "").strip(), as_str(args, "foreman_notes"), note) if part
	]
	doc.foreman_notes = "\n\n".join(lines)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	closed = dict(doc.as_dict())
	described = shifts.describe(closed, with_children=True)
	# THE BRIDGE RUNS AFTER THE CLOSE AND CANNOT UNDO IT, exactly as it does in
	# `end_shift`. The crew worked; a close that wrote no Attendance would be
	# wrong in the employer's favour, which is the failure this app names.
	bridge = shifts.bridge_to_attendance(closed, shifts.crew_of(row["name"]))
	hours = shifts.hours_between(start, end)

	data = {
		**described,
		"actor": actor,
		"shift_hours": hours,
		"hours_open_before_close": open_for,
		"stale_after_hours": threshold,
		"closure_reason": reason,
		"end_datetime_assumed": end_assumed,
		"assumed_shift_hours": ASSUMED_SHIFT_HOURS if end_assumed else None,
		"workers_released": released,
		"workers_released_count": len(released),
		"attendance": bridge,
		"attendance_created": len(bridge.get("created") or []),
		"supervisor_review_owed": not signature,
		"note": (
			f"{row['name']} is CLOSED at {end} after {open_for} hour(s) open. "
			f"{len(released)} crew member(s) were released — their rows are KEPT with left_at "
			"set, which is the same storage remove_worker_from_shift uses and is the record a "
			f"wage claim turns on. {len(bridge.get('created') or [])} Attendance record(s) were "
			"written, each spanning that person's own joined_at to this end time."
		),
	}
	if signature:
		data["review_note"] = (
			"A supervisor signature WAS passed, so this close is an attestation under FSMA "
			"§112.161(b) and stands exactly as an end_shift close does."
		)
	else:
		data["review_note"] = (
			"NO SUPERVISOR SIGNATURE IS ON THIS CLOSE and none was invented. FSMA §112.161(b) "
			"asks for a review that is dated and signed by a supervisor or responsible party, "
			"and this record does not have one — the shift is closed, the crew is paid, and "
			"the attestation is OWED. That is the honest state of a shift ended days later by "
			"somebody who was not there, and it is better evidence than a signature dated "
			"today against a Tuesday nobody reviewed. Where the supervisor IS available, pass "
			"supervisor_signature_file_token on this call: once the shift is closed, end_shift "
			"will not reopen it to add one."
		)
	if end_assumed:
		data["end_time_note"] = (
			f"NOBODY STATED WHEN WORK STOPPED, so this close assumed {ASSUMED_SHIFT_HOURS} hours "
			f"from the start and ended the shift at {end}. That is an ASSUMPTION and it is on "
			"the record as one — it reached every crew member's Attendance row and therefore "
			"their pay. Where somebody knows the real time, pass end_datetime; where the "
			"assumption turns out to be wrong, it is the Attendance rows that need correcting, "
			"because this shift will not close a second time."
		)
	if not released:
		data["crew_note"] = (
			"Every crew member already had a left_at, so nobody needed releasing — this was a "
			"shift whose crew was clocked out one by one and whose ENDING was never written. "
			"That is the ordinary shape of a foreman who closed the crew screen instead of the "
			"shift."
		)
	return ToolResult(
		data=data,
		summary=(
			f"closed stale {row['name']} at {end} after {open_for} hour(s) open; "
			f"{len(released)} worker(s) released; "
			f"{len(bridge.get('created') or [])} Attendance record(s) written"
			+ (f"; end time ASSUMED ({ASSUMED_SHIFT_HOURS}h from start)" if end_assumed else "")
			+ ("" if signature else "; supervisor review OWED")
		),
		docstatus_delta="0 → 0 (closed, administratively)",
	)


def _resolve_stale_shift(args: dict) -> dict:
	"""The shift, under its row lock, re-read after the lock was taken.

	THE SAME CONTRACT `tools/shifts._resolve_shift_for_update` HAS AND FOR THE
	SAME REASON: `shifts.lock_shift` makes a read authoritative and cannot
	refresh one already taken, so the row this tool decides on must be read
	AFTER the lock. Frappe rewrites a child table by deleting and re-inserting
	its rows, so a tool that saves the shift document outside the lock can drop a
	worker off a roster — and this tool saves the crew.
	"""
	from .shifts import _resolve_shift_for_update

	return _resolve_shift_for_update(args)

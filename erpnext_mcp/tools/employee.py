# SPDX-License-Identifier: MIT
"""The personnel register: creating an Employee, editing one, and linking it to a login.

v0.18.1. THIS FILE EXISTS BECAUSE THE MOBILE APP WORKED AND THE DATA DID NOT.

v0.18.0 got a phone all the way through the funnel, the credential and the eleven
methods — and then `list_my_tasks` refused it, correctly, with "set user_id on
their Employee record to this email address". Every Farm Ops method scopes work by
EMPLOYEE, not by User: a task board is a list of what a *person* is assigned, and
the Employee record is the only thing on a Frappe site that says which person a
login is. So an account with no Employee behind it is an account with nothing to
show, and the refusal was right.

What was missing was any way to fix it from here. This app could create the User,
the roles, the entity scoping, the grant, the credential and the QR — six things —
and could not create or edit the ONE record that makes the other six useful. An
operator had to leave the conversation, open the Desk, and find a form. That is
the gap, and these three tools are it:

  * `create_employee`      — the record itself.
  * `update_employee`      — the fields on it that a new hire's first week changes.
  * `link_employee_to_user` — the one field that turns a login into a person.

v0.46.2 added a fourth, and it is the only READ here: `get_employee`, one record
with the onboarding state a RETURNING worker arrives with. It is in this file
rather than in `hr.py` with `list_employees` because answering it means knowing
that `i9_status` and `w4_status` are this app's own columns, that nothing here
ever moves them, and which values may therefore be reconciled against the I-9
and W-4 records — all of which is this module's business and none of which is
the HR register's. See its own docstring; the argument is the long one.

`onboard_employee` in `newhire.py` orchestrates all three plus the mobile account
and the QR; it is the tool to reach for when the person is new. These are the
tools for when they are not.

────────────────────────────────────────────────────────────────────────────
IT WRITES TWENTY-TWO FIELDS AND REFUSES EVERYTHING ELSE BY NAME
────────────────────────────────────────────────────────────────────────────

`WRITABLE` is a closed list. Not a filter over the doctype, not "everything
except" — a list, written out, that a reader can check against what they expected.
Everything on it is an identity or assignment fact: who this person is, which
entity hired them, what they do, when they started, how to reach them.

Fourteen until v0.46.1, when the three below joined them. See the next section:
they are on the list because this app is the reason they are mandatory, and a
list that cannot write them is a list that cannot hire.

Eighteen until v0.54.0, when `branch` joined them. It is Frappe HR's own
operating-unit dimension and the only field the hiring wizard's Assignment step
asked for that this list did not carry — so every hire made through the wizard
recorded which COMPANY employs somebody and nothing about which camp they report
to, and a dispatcher looking for the Mill Creek crew had to read it off the
housing assignment or ask.

Nineteen until v0.62.0, when the current address and the two halves of the
emergency contact joined them for the same reason one release later. The mobile
surface's `set_employee_contact_fields` names five contact fields and this list
carried two of them, so a phone number and an email could be filed on a hire day
and the person to ring if that picker went down on a block could not.

Everything NOT on it is refused, and the payroll, tax and banking fields are
refused with their OWN message, because those are the ones somebody will actually
try. A salary structure, an income tax slab and a bank account number each have a
form, an approval and a retention rule around them that this app knows nothing
about, and a tool that let a language model set `ctc` because it appeared in a
sentence would be a tool that quietly moved money.

────────────────────────────────────────────────────────────────────────────
THE SCHEMA IS ASKED, NEVER ASSUMED
────────────────────────────────────────────────────────────────────────────

Frappe HR's Employee doctype is not the same on every site and is not the same on
every version. `gender` and `employment_type` are Links on a stock install and
Selects on some customised ones; `date_of_birth` and `gender` are MANDATORY on a
stock Frappe HR and are made optional by plenty of operators; a site that never
installed HR's masters has no Department records at all.

So: every field is checked against `frappe.get_meta` before it is written, every
Link target comes off the field's own `options` (with `LINK_TARGETS` only as the
fallback for a site whose meta says nothing), every Select is matched
case-insensitively against the site's own choices, and a Link whose target doctype
does not exist here is NOT validated rather than refused — the double cannot tell
"missing record" from "missing schema", and neither can this.

And the mandatory fields are the site's, not this file's. `_mandatory_gaps` reads
`reqd` off the meta and is used to turn Frappe's `MandatoryError` — which arrives
as a wall of HTML from a controller — into a sentence naming the fields this site
wants. That is the difference between "tell me what to send" and "something went
wrong in hrms".

────────────────────────────────────────────────────────────────────────────
EXCEPT FOR THE THREE THIS APP MADE MANDATORY ITSELF
────────────────────────────────────────────────────────────────────────────

v0.46.1. The mandatory-field message read "this site's Frappe HR marks i9_status,
w4_status, jurisdiction mandatory on Employee", and it was wrong about whose
decision that was. Frappe HR has never heard of those three: `compliance_fields.py`
installs them as Custom Fields with `reqd=True`, so the sentence "which fields are
required is an operator's decision on the doctype, not this app's" was this app
telling a caller to go argue with itself.

It was not a message problem. It was a wall in front of EVERY creation path —
`create_employee`, `onboard_employee`, and the iOS wizard's first step, which is
where it finally surfaced — and it had one correct answer, which is that a person
hired ten seconds ago has a known state on all three:

  * `i9_status` is `Pending`. The I-9 is a separate form with a three-business-day
    clock on it, and `create_i9_form` is a later step of the same wizard. Pending
    is what the alert engine watches for and what an unverified hire actually is.
  * `w4_status` is `Missing` — not "Pending", which is not one of the three
    options the field carries. `docs/compliance_fields.md` defines Missing as "the
    employer withholds at the default single rate", which is precisely true of
    somebody whose W-4 has not been submitted yet. `submit_w4` moves it to On-File.
  * `jurisdiction` is read off the hiring entity's own address, falling back to
    `DEFAULT_JURISDICTION`. Wage law follows where the WORK is, which no record
    knows at hire time; the entity's state is the closest honest starting value.

THE SAFETY NET IS NOT REMOVED. `_mandatory_gaps` still runs and still refuses,
which is what should happen when an operator marks `date_of_birth` or `gender`
required — those are facts about a person that nobody can default, and inventing
one would be worse than the refusal. What changed is only that this app fills in
the fields this app required. All three are on `WRITABLE`, so a caller who knows
better passes their own value and no default is applied; every default that IS
applied is named in `defaults_applied` and in the note, because a record that
quietly acquired a compliance status is exactly the record nobody goes back to fix.

────────────────────────────────────────────────────────────────────────────
ONE PERSON, ONE EMPLOYEE, ONE LOGIN
────────────────────────────────────────────────────────────────────────────

`Employee.user_id` is a one-to-one in every direction that matters. Two Employee
records naming the same User puts somebody on the dispatch board twice and in the
payroll register once — `newhire.py` has argued that at length since v0.17.1 — and
`list_my_tasks` resolving a login to two employees has no correct answer to give.
So the link is refused when the User already belongs to somebody else, and
refused when the Employee already belongs to somebody else, and is a NO-OP when it
is already exactly what was asked for. Re-running is safe; re-pointing is a
decision (`replace=true`).

A link to a User with no Farm Ops role and no Mobile Access Grant is refused too,
and that refusal is the least obvious one here so it is worth the sentence: the
link's entire purpose is to make the mobile methods answer. A User who cannot
reach them is a link that changes nothing today and silently grants a task board
on the day somebody grants that account a role for an unrelated reason.
`allow_unenrolled_user=true` says "I know, I am doing this ahead of the grant",
which is a real order of operations and is why the escape exists.
"""

from __future__ import annotations

import frappe

from .. import compat, minors, roles, security
from ..args import as_bool, as_choice, as_date, as_str, resolve_company, select_options
from ..errors import ToolError
from ..result import ToolResult

EMPLOYEE = "Employee"
USER = "User"
GRANT = "Mobile Access Grant"

#: Who may touch the personnel register through this app.
#:
#: `Administrator` holds every role Frappe has, so the default configuration —
#: where `effective_user` falls back to Administrator — passes. An operator who
#: pointed `mcp_system_user` at a purpose-built account gets a refusal that NAMES
#: the account and these four roles, because "permission denied" on a principal
#: the operator chose themselves is a one-line fix they cannot make without
#: knowing which line.
#:
#: "Farm Manager" is this app's own role and is here because on this site it is
#: the person who actually hires. "HR User" is Frappe HR's own read/write role and
#: is the one a real HR clerk holds; "HR Manager" is the one an operator assumes
#: is required and would be surprised to find missing.
HR_ROLES = ("System Manager", "HR Manager", "HR User", "Farm Manager")

#: Who may run a crew shift — form it, log what happened on it, and sign it off.
#:
#: `HR_ROLES` PLUS THE TWO ROLES THAT ARE ACTUALLY STANDING ON THE BLOCK, and
#: built from that tuple rather than beside it so the two lists cannot drift.
#:
#: THE GAP THIS CLOSES WAS A HANDSET SHOWING A BUTTON THE SERVER REFUSED.
#: `ShiftToolsToolbar` offers Crew Clock to Farm Manager, System Manager, Foreman
#: and Crew Leader, and the shift tools gated on `HR_ROLES`, which has neither of
#: the last two — so a foreman who tapped it was told they "may not change the
#: personnel register" for the one record that is unambiguously theirs.
#:
#: IT IS NOT A LOOSENING OF THE PERSONNEL GATE, and the two lists are separate
#: for that reason. A Foreman still cannot hire, edit an Employee, touch an I-9
#: or a W-4, or read the wage tables — `create_employee` and everything beside it
#: keep `HR_ROLES` exactly as it was. What this adds is the register OAR
#: 437-004-1131 already puts on them: the water, shade, rest-cycle and
#: observation obligations sit on the NAMED supervisor, `roles.py` grants Foreman
#: FULL permission on `Farm Shift` for exactly that reason, and a supervisor who
#: cannot open the shift cannot discharge the obligation the rule names them for.
#:
#: THE ATTENDANCE ROWS ARE THE POINT OF THE CLOSE. `end_shift` writes one
#: submitted Attendance per crew member from their own `joined_at`/`left_at`, so
#: a foreman who cannot close is a crew with no wage record for the day.
#:
#: "Crew Leader" IS NOT ONE OF THE SIX ROLES `roles.py` SHIPS. It is a role
#: operators create by hand on sites where the crew lead is not the foreman, and
#: naming it here costs nothing on a site that has not got one — `frappe.get_roles`
#: simply never returns it — while a site that does have one stops being a site
#: where the app's own iOS client offers a button its server refuses.
SHIFT_ROLES = (*HR_ROLES, "Foreman", "Crew Leader")

#: Who may bring a person onto the farm and complete their first day.
#:
#: THERE IS NO PERSONNEL OFFICE ON A FARM THIS SIZE. The foreman IS the back
#: office — the farmer, the crew, and traditionally somebody doing the hiring
#: paperwork at a kitchen table on Sunday. A gate that sent the foreman looking
#: for an HR department would be a gate that stops the hire, and the record that
#: eventually got typed in on Sunday is worse evidence than the one collected
#: standing next to the person it is about.
#:
#: NOT THE SAME QUESTION AS WHO MAY READ THE PERSONNEL REGISTER, which stays
#: `HR_ROLES`: `search_employees`, `get_employee` and `update_employee` are
#: unchanged, because reading or editing an existing personnel record is somebody
#: else's PII and the reason the role gates exist at all.
#:
#: NOT THE SAME QUESTION AS WHO MAY MAKE THE EMPLOYER'S I-9 ATTESTATION either.
#: That is a per-PERSON designation on the authorized-signer roster (`tools/
#: signers.py`) and not a role at all — USCIS lets an employer designate an
#: authorized representative, so the foreman the farm names can lawfully complete
#: Section 2 and a foreman it has not named cannot, whatever role they hold.
#:
#: EQUAL TO `SHIFT_ROLES` BY POLICY, NOT BY DEFINITION, and spelled with its own
#: name for that reason: the day somebody decides a Crew Leader may run a shift
#: but not hire, this is the one line that changes and nothing else moves.
#:
#: WHAT REPLACES THE GATE IS NOT NOTHING. `create_employee` still refuses a
#: one-word name; `require_company_scope` still runs immediately afterwards, so a
#: foreman hires into their own entity and not the holding company's; the I-9
#: status machine makes an incomplete hire visibly incomplete; and every write
#: below records the actor. Attribution is what this widening leans on.
HIRING_ROLES = SHIFT_ROLES


def _farm_ops_roles() -> frozenset:
	"""The roles that make a login worth linking to an Employee.

	`api/guard.FARM_OPS_ROLES` itself — read rather than re-listed, so the set this
	file refuses against and the set the eleven mobile methods gate on cannot drift
	apart. Imported inside the function because `api/guard` imports the tool layer
	in turn, and a module-level import here would close that circle at load time.
	"""
	from ..api import guard

	return guard.FARM_OPS_ROLES


#: Every field these tools will write, and the doctype each Link points at when
#: this site's meta does not say. See the module docstring on why this is a
#: closed list rather than a filter.
LINK_TARGETS = {
	"branch": "Branch",
	"company": "Company",
	"department": "Department",
	"designation": "Designation",
	"employment_type": "Employment Type",
	"gender": "Gender",
	# v0.68.1. THE SUPERVISOR, AND IT POINTS AT ANOTHER EMPLOYEE. Frappe HR's own
	# `Employee.reports_to`, and the only self-referential Link in this table —
	# which is why `_reports_to_or_refuse` below does a check none of the others
	# needs: a Link that can name its own row can also name a cycle, and a cycle
	# is not a data-entry curiosity here. `shadow_log.py` WALKS this chain to
	# decide who reviews whom, and `dispatch.py` reads one hop of it to find a
	# worker's supervisor; both survive a cycle by reporting it, and neither
	# should have to.
	"reports_to": EMPLOYEE,
	"user_id": USER,
}

DATE_FIELDS = ("date_of_joining", "date_of_birth")

SELECT_FIELDS = ("status", "i9_status", "w4_status")

#: The three fields `compliance_fields.py` installs on Employee with `reqd=True`,
#: in the order the mandatory-field refusal used to list them. This app is the
#: reason a site has them and the reason they are required, so this app is what
#: has to have an answer for them — see the module docstring.
COMPLIANCE_FIELDS = ("i9_status", "w4_status", "jurisdiction")

#: The twenty-two. Ordered as somebody filling in a form would read them, because
#: this tuple is what the refusal messages list.
WRITABLE = (
	"employee_name",
	"first_name",
	# v0.51.0. An identity fact, and the one that was missing. The handset has
	# read a middle name off every AAMVA licence barcode since the ID scanner
	# shipped — `DAD`, or `DBN` where a jurisdiction uses that — and it could
	# not be written here, so it was parsed and dropped. That also silently cost
	# the I-9: `submit_i9_section_1` fills Legal Middle Name from
	# `Employee.middle_name` when the caller does not send one, and the column
	# it read was always empty.
	"middle_name",
	"last_name",
	"company",
	"date_of_joining",
	"date_of_birth",
	"gender",
	"department",
	"designation",
	"employment_type",
	# v0.54.0. Frappe HR's own operating-unit dimension on Employee, and the one
	# field the hiring wizard's Assignment step asked for that this allowlist did
	# not carry — so a crew hired for the Mill Creek camp came out with the ranch
	# in `company` and nothing saying which camp. It is a Link on a stock HR
	# Employee; `_clean` asks THIS site's meta what it points at before it checks
	# anything, so a site that customised it into a Data field is not refused, and
	# a site without Frappe HR's Branch master reports it through
	# `fields_not_on_this_site` rather than failing the hire.
	"branch",
	# v0.68.1. WHO THEY REPORT TO, and it joins this list under exactly the rule
	# `branch` joined it under: a field two of this app's own surfaces already
	# READ and nothing could WRITE. `dispatch.py` refuses to escalate a task with
	# "no reports_to on their Employee record, so this app does not know who
	# their supervisor is", and `shadow_log.py` walks the chain to build a review
	# ladder — both of them against a column whose only editor was the Desk.
	#
	# IT IS AN ASSIGNMENT FACT AND NOT A PAY ONE. A supervisor is who signs the
	# shift and who the escalation goes to; it sets no rate, no structure and no
	# approval limit, which is why it belongs on this list and not on
	# `SENSITIVE_FIELDS` beside `salary_structure`.
	"reports_to",
	"status",
	"user_id",
	"personal_email",
	"cell_number",
	# v0.62.0. THE THREE COLUMNS "HOW TO REACH THEM" WAS MISSING, and they join
	# the list under the same rule as `branch` did: a field the hiring wizard's
	# own step declares and this list cannot write is a field collected on a
	# tailgate and dropped on arrival. `set_employee_contact_fields` on the mobile
	# surface names all five contact fields — a cell number, a personal email, a
	# current address and the two halves of an emergency contact — and only the
	# first two had anywhere to land.
	#
	# THEY ARE FRAPPE HR'S OWN SPELLINGS, not the app's. `person_to_be_contacted`
	# is labelled "Emergency Contact Name" on the form and `emergency_phone_number`
	# is "Emergency Phone"; the mobile wrapper takes the labels the handset speaks
	# and maps them here, because the docname of a column is not a thing a phone
	# should have to know.
	#
	# NONE OF THE THREE IS PAYROLL, TAX OR BANKING — see `SENSITIVE_FIELDS`. They
	# are the same kind of fact as the two above them: how somebody is reached,
	# and who is called if something happens to them on a block. An operation that
	# cannot answer the second question at four in the afternoon in August has a
	# real problem, and the answer is collected on the day somebody is hired or it
	# is not collected at all.
	"current_address",
	"person_to_be_contacted",
	"emergency_phone_number",
	*COMPLIANCE_FIELDS,
)

#: Fields refused with their own sentence rather than the generic one. Not a
#: security boundary — everything outside `WRITABLE` is refused either way — but
#: the message is the difference between a caller who understands why and a caller
#: who tries a synonym. Payroll, tax and banking: each has a form, an approval and
#: a retention rule this app knows nothing about.
SENSITIVE_FIELDS = frozenset(
	{
		"bank_ac_no",
		"bank_name",
		"ctc",
		"iban",
		"income_tax_slab",
		"payroll_cost_center",
		"provident_fund_account",
		"salary_currency",
		"salary_mode",
		"salary_structure",
		"tax_withholding_category",
	}
)

#: What a new Employee is unless the caller says otherwise.
DEFAULT_STATUS = "Active"

#: What a person hired ten seconds ago IS on the two Select fields this app made
#: mandatory. Both are the option that means "the form has not been filed yet",
#: matched to the options `compliance_fields.py` installs — `w4_status` has no
#: `Pending`, and `Missing` is the one its own documentation defines as "the
#: employer withholds at the default single rate", which is the true state of a
#: hire whose W-4 step has not run. Checked against the SITE's options anyway, by
#: `_clean`, because an operator may have edited them.
COMPLIANCE_DEFAULTS = {
	"i9_status": "Pending",
	"w4_status": "Missing",
}

#: The wage-law state when the hiring entity's own address does not say. Oregon,
#: because ORS 653 is the law `state_withholding.py` was built against and this
#: app's operations are there. An entity anywhere else records an address and gets
#: its own state; an entity with no address at all gets a value an operator can
#: see and correct, which beats a hire that cannot happen.
DEFAULT_JURISDICTION = "OR"

#: Full state names to the two-letter codes `jurisdiction` is documented to carry.
#: ERPNext's Address stores whichever form the operator typed, and a Washington
#: entity recorded as "Washington" and then paid under ORS 653 is exactly the
#: mistake this map exists to stop — the two states differ on agricultural
#: overtime, on rest breaks and on minimum wage regions.
US_STATE_CODES = {
	state.lower(): code
	for code, state in (
		("AL", "Alabama"),
		("AK", "Alaska"),
		("AZ", "Arizona"),
		("AR", "Arkansas"),
		("CA", "California"),
		("CO", "Colorado"),
		("CT", "Connecticut"),
		("DE", "Delaware"),
		("DC", "District of Columbia"),
		("FL", "Florida"),
		("GA", "Georgia"),
		("HI", "Hawaii"),
		("ID", "Idaho"),
		("IL", "Illinois"),
		("IN", "Indiana"),
		("IA", "Iowa"),
		("KS", "Kansas"),
		("KY", "Kentucky"),
		("LA", "Louisiana"),
		("ME", "Maine"),
		("MD", "Maryland"),
		("MA", "Massachusetts"),
		("MI", "Michigan"),
		("MN", "Minnesota"),
		("MS", "Mississippi"),
		("MO", "Missouri"),
		("MT", "Montana"),
		("NE", "Nebraska"),
		("NV", "Nevada"),
		("NH", "New Hampshire"),
		("NJ", "New Jersey"),
		("NM", "New Mexico"),
		("NY", "New York"),
		("NC", "North Carolina"),
		("ND", "North Dakota"),
		("OH", "Ohio"),
		("OK", "Oklahoma"),
		("OR", "Oregon"),
		("PA", "Pennsylvania"),
		("PR", "Puerto Rico"),
		("RI", "Rhode Island"),
		("SC", "South Carolina"),
		("SD", "South Dakota"),
		("TN", "Tennessee"),
		("TX", "Texas"),
		("UT", "Utah"),
		("VT", "Vermont"),
		("VA", "Virginia"),
		("WA", "Washington"),
		("WV", "West Virginia"),
		("WI", "Wisconsin"),
		("WY", "Wyoming"),
	)
}


# ── the guards every tool in this file runs first ───────────────────────────
def require_hr_role() -> str:
	"""The principal this call is attributed to, once it has proved it may hire.

	Returns the user so a caller has one thing to hold and to log. Raises rather
	than returning a flag: there is no half-permitted path through any of these.

	WHICH IDENTITY. `security.caller_identity()` is whoever Frappe authenticated
	THIS request — a phone or a Desk session presenting its own credential — and
	is empty on the ordinary MCP path, where the operator's client presents a
	shared token and no human identity exists to read. There the principal is
	`frappe.session.user`, which by the time any tool runs is the MCP System User
	the operator configured. Both are real principals whose roles the operator
	controls, and gating on whichever one is present is what makes this check mean
	something on both transports instead of meaning nothing on one.
	"""
	return _require_one_of(HR_ROLES, "a personnel change", "change the personnel register")


def require_shift_role() -> str:
	"""The principal this call is attributed to, once it has proved it may run a shift.

	`require_hr_role` WITH TWO MORE ROLES AND NOTHING ELSE DIFFERENT — same
	identity resolution, same refusal shape, same company scope applied by the
	caller afterwards. See `SHIFT_ROLES` for why the crew's own supervisor is on
	a list the personnel register's gate does not have.
	"""
	return _require_one_of(SHIFT_ROLES, "a shift record", "form or close a crew shift")


def require_hiring_role() -> str:
	"""The principal this call is attributed to, once it has proved it may hire.

	`require_hr_role` WITH THE TWO SUPERVISOR ROLES AND NOTHING ELSE DIFFERENT —
	same identity resolution, same refusal shape, same company scope applied by
	the caller afterwards. See `HIRING_ROLES` for why bringing somebody onto the
	farm is a different question from reading the register they land in.
	"""
	return _require_one_of(HIRING_ROLES, "a hire", "bring a person onto the farm or complete their first day")


def _require_one_of(allowed: tuple, attribution: str, refusal: str) -> str:
	"""The shared body of the two gates above: one identity, one role set.

	WHICH IDENTITY. `security.caller_identity()` is whoever Frappe authenticated
	THIS request — a phone or a Desk session presenting its own credential — and
	is empty on the ordinary MCP path, where the operator's client presents a
	shared token and no human identity exists to read. There the principal is
	`frappe.session.user`, which by the time any tool runs is the MCP System User
	the operator configured. Both are real principals whose roles the operator
	controls, and gating on whichever one is present is what makes this check mean
	something on both transports instead of meaning nothing on one.
	"""
	actor = security.caller_identity() or str(getattr(frappe.session, "user", "") or "")
	if not actor or actor == "Guest":
		raise ToolError(f"this call has no identity to attribute {attribution} to. Nothing was changed.")
	held = set(frappe.get_roles(actor) or []) or set(roles.all_roles_of(actor) or [])
	if not held & set(allowed):
		raise ToolError(
			f"{actor} may not {refusal}: it holds none of "
			f"{', '.join(allowed)}. This is the account this app acts as — an operator "
			"sets it with `mcp_system_user` on ERPNext MCP Settings, and grants it a role "
			"in the Desk. Nothing was changed."
		)
	return actor


def require_company_scope(actor: str, company: str) -> str:
	"""One Company, checked against what this principal may actually reach.

	FRAPPE'S RULE, NOT THE MOBILE SURFACE'S. A principal with no Company User
	Permission is UNRESTRICTED here, which is what every Desk surface on the site
	already does and what makes an operator's own login work. `api/guard.py`
	deliberately inverts that for the eleven mobile methods and says why at
	length: those face the open internet with a closed list of callers, where
	fail-closed is affordable. This does not, and a personnel tool that refused
	every correctly-configured operator would be turned off within the hour.

	Where the principal IS scoped, the scope is enforced: creating an Employee for
	an entity you cannot see is exactly the mistake this checks for.
	"""
	allowed = roles.companies_for(actor) or []
	if allowed and company not in allowed:
		raise ToolError(
			f"{actor} has no access to company {company!r} — its entity access is "
			f"{', '.join(allowed)}. An Employee belongs to the entity that hired them, and "
			"creating one for an entity you cannot see would put a person on a payroll "
			"register you cannot read. Nothing was changed."
		)
	return company


# ── name resolution ─────────────────────────────────────────────────────────
def resolve_employee(value: str) -> str:
	"""An Employee docname from a docname, an employee_number, a name, or a login.

	Four ways in because those are the four things somebody calls "the employee",
	and only the first is the primary key. Ambiguity is reported with the
	candidates rather than resolved by guessing — two people called Ana Ramos is a
	real situation and picking one of them silently is the worst available answer.
	"""
	value = (value or "").strip()
	if not value:
		raise ToolError("employee is required (its docname, employee number, name, or linked login).")
	if frappe.db.exists(EMPLOYEE, value):
		return value
	for filters in ({"employee_number": value}, {"employee_name": value}, {"user_id": value.lower()}):
		field = next(iter(filters))
		if not compat.has_field(EMPLOYEE, field):
			continue
		matches = frappe.db.get_all(EMPLOYEE, filters=filters, pluck="name", limit=10)
		if len(matches) == 1:
			return matches[0]
		if len(matches) > 1:
			raise ToolError(
				f"{value!r} matches {len(matches)} employees: {', '.join(sorted(matches))}. "
				"Pass the Employee docname. Nothing was changed."
			)
	raise ToolError(
		f"no Employee matching {value!r} (tried docname, employee_number, employee_name and "
		"user_id). list_employees has every one on this site. Nothing was changed."
	)


# ── field validation ────────────────────────────────────────────────────────
def _link_target(fieldname: str) -> str:
	"""What a Link field points at, asked of the site before assumed.

	The site's own `options` wins, because an operator who re-pointed
	`Employee.designation` at a doctype of their own is right and this file is not.
	`LINK_TARGETS` is the fallback for a meta that says nothing, which is what a
	Data field customised into place looks like.
	"""
	field = compat.field_meta(EMPLOYEE, fieldname)
	fieldtype = str((field or {}).get("fieldtype") or "")
	if fieldtype in ("Link", "Dynamic Link"):
		target = str((field or {}).get("options") or "").strip()
		if target:
			return target
	if fieldtype in ("Select", "Data"):
		# Explicitly not a Link on this site. Nothing to check a record against.
		return ""
	return LINK_TARGETS.get(fieldname, "")


def _clean(fieldname: str, raw, label: str = "") -> str:
	"""One field value, coerced and checked against this site's schema.

	Returns the value to write. Raises with the site's own choices listed for
	anything it cannot accept — a caller that is a language model can act on
	"must be one of: Active, Inactive, Suspended, Left" and cannot act on a
	controller traceback.
	"""
	label = label or fieldname
	if fieldname in DATE_FIELDS:
		return as_date({fieldname: raw}, fieldname) or ""

	value = str(raw or "").strip()
	if not value:
		return ""

	if fieldname == "user_id":
		value = value.lower()

	if fieldname == "reports_to":
		# RESOLVED, NOT MATCHED. Every other Link here is a master whose docname
		# is the thing somebody types; this one points at an Employee, whose
		# docname is "HR-EMP-00042" and is the one spelling nobody knows. The
		# same four ways in that `resolve_employee` gives every other tool —
		# docname, employee number, name, login — apply here, or a foreman would
		# be the only field on the form you cannot fill in from the badge.
		return resolve_employee(value)

	if fieldname in SELECT_FIELDS and select_options(EMPLOYEE, fieldname):
		return as_choice(EMPLOYEE, fieldname, value, label)

	target = _link_target(fieldname)
	if target and compat.doctype_exists(target) and not frappe.db.exists(target, value):
		# A Link whose target doctype is ABSENT is deliberately not checked: the
		# value cannot be verified against a schema that is not here, and refusing
		# would refuse a perfectly good value on a site that simply never installed
		# HR's masters. Frappe does not validate that link either.
		raise ToolError(
			f"{label} {value!r} is not a {target} on this site. {_known(target)} Nothing was changed."
		)
	return value


def _known(doctype: str) -> str:
	"""A few real values of `doctype`, so a refusal is also an answer."""
	try:
		names = frappe.db.get_all(doctype, pluck="name", limit=12, order_by="name asc") or []
	except Exception:  # pragma: no cover - a doctype with no name column
		return ""
	if not names:
		return f"This site has no {doctype} records at all — create one first."
	return f"Known {doctype}: {', '.join(str(name) for name in names)}."


def _reject_unknown(args: dict, allowed: tuple, reserved: tuple = ()) -> None:
	"""Refuse a field this tool does not write, and say which kind of no it is."""
	for key in args:
		if key in allowed or key in reserved:
			continue
		if key in SENSITIVE_FIELDS:
			raise ToolError(
				f"{key!r} is a payroll, tax or banking field, and this app does not write those. "
				"A salary structure, an income tax slab and a bank account number each have a "
				"form, an approval and a retention rule around them that this app knows nothing "
				"about. Set it in the Desk, where the HR module's own checks run. Nothing was "
				"changed."
			)
		if not compat.has_field(EMPLOYEE, key):
			raise ToolError(
				f"{key!r} is not an argument this tool takes, and is not a field on this site's "
				f"Employee doctype either. The fields it writes are: {', '.join(WRITABLE)}. "
				"Nothing was changed."
			)
		raise ToolError(
			f"{key!r} is a real Employee field on this site but is not one this tool writes. "
			# COUNTED RATHER THAN SPELLED OUT. It was "the nineteen" in the
			# sentence and in the docstring until v0.62.0 added three, and a
			# refusal that names a number the tuple no longer has is a refusal
			# that teaches the reader something false about the allowlist.
			f"The {len(WRITABLE)} it does are: {', '.join(WRITABLE)}. Anything else belongs in "
			"the Desk, where the HR module's own validation runs. Nothing was changed."
		)


def _supported(payload: dict) -> tuple:
	"""Split a payload into what this site's Employee has and what it does not.

	A field the doctype does not carry is NOT written and NOT silently dropped —
	it is reported, because "this Frappe HR does not have `cell_number`" is a fact
	about the site the caller needs, and a tool that swallowed it would report
	success for a value that went nowhere.
	"""
	writable, absent = {}, []
	for key, value in payload.items():
		if compat.has_field(EMPLOYEE, key):
			writable[key] = value
		else:
			absent.append(key)
	return writable, absent


def _mandatory_gaps(payload: dict) -> list:
	"""Fields this site marks mandatory on Employee that the payload has not filled.

	Read off the meta rather than listed here, because which fields are required
	is an operator's decision: stock Frappe HR requires `gender` and
	`date_of_birth` and plenty of sites make both optional.

	CHECKED BEFORE THE INSERT RATHER THAN AFTER IT. Frappe would refuse the same
	record a moment later, so refusing here costs nothing and buys a sentence
	naming the fields instead of a controller's `MandatoryError`. `_insert` keeps
	the same message as a catch, for a requirement a controller imposes in code
	that the meta does not express.
	"""
	try:
		fields = frappe.get_meta(EMPLOYEE).fields
	except Exception:  # pragma: no cover - no meta for Employee
		return []
	gaps = []
	for field in fields:
		if not compat.checked(field.get("reqd")):
			continue
		name = str(field.get("fieldname") or "")
		if not name or field.get("default") or name in ("naming_series", "employee"):
			continue
		if not str(payload.get(name) or "").strip():
			gaps.append(name)
	return sorted(gaps)


# ── the linkage rules ───────────────────────────────────────────────────────
def linkage_state(user: str) -> dict:
	"""Everything that decides whether linking this User is worth doing.

	Returned in the result of all three tools, because "the link exists" and "the
	phone will now get a task list" are different facts and only the second is the
	one somebody actually wanted.
	"""
	held = set(roles.all_roles_of(user) or [])
	grant = {}
	if compat.doctype_exists(GRANT):
		grant = (
			frappe.db.get_value(GRANT, {"user": user}, ["name", "state", "mobile_role"], as_dict=True) or {}
		)
	farm_ops = sorted(held & _farm_ops_roles())
	return {
		"user": user,
		"roles_held": sorted(held),
		"farm_ops_roles": farm_ops,
		"grant": grant.get("name"),
		"grant_state": grant.get("state"),
		"entity_access": roles.companies_for(user),
		"farm_ops_ready": bool(farm_ops) and str(grant.get("state") or "") == "Active",
	}


def _require_enrolled(user: str, args: dict) -> dict:
	"""A User worth linking, or the reason it is not one.

	See the module docstring. The escape is real and named, because enrolling
	after the Employee exists is a legitimate order of operations — it is what
	`onboard_employee` does, in that order, on purpose.
	"""
	state = linkage_state(user)
	if as_bool(args, "allow_unenrolled_user", False):
		return state
	if state["farm_ops_ready"] or state["farm_ops_roles"]:
		return state
	raise ToolError(
		f"{user} has no Farm Ops role and no Mobile Access Grant, so linking it to an Employee "
		"would change nothing today: the eleven mobile methods gate on "
		f"{', '.join(sorted(_farm_ops_roles()))} and on an Active grant, and this account has "
		"neither. Run create_mobile_user for it first — or pass allow_unenrolled_user=true if "
		"you are deliberately linking ahead of the grant. Nothing was changed."
	)


def _require_free_user(user: str, employee: str = "") -> None:
	"""Refuse a User that already belongs to somebody else. One person, one login."""
	if not compat.has_field(EMPLOYEE, "user_id"):
		return
	holder = frappe.db.get_value(EMPLOYEE, {"user_id": user}, "name")
	if holder and str(holder) != employee:
		name = frappe.db.get_value(EMPLOYEE, holder, "employee_name") or holder
		raise ToolError(
			f"{user} is already the login for Employee {holder} ({name}). Employee.user_id is a "
			"one-to-one: two Employee records naming one login puts somebody on the dispatch "
			"board twice, and gives list_my_tasks two answers where it needs one. Unlink that "
			"record first with update_employee, or use a different address. Nothing was changed."
		)


# ── 1. create_employee ──────────────────────────────────────────────────────
def create_employee(args: dict) -> ToolResult:
	"""One Employee record, with the site's own schema as the arbiter of every field."""
	compat.require_doctype(EMPLOYEE, "It comes with the Frappe HR (hrms) app.")
	_reject_unknown(args, WRITABLE, reserved=("allow_unenrolled_user", "allow_duplicate_name"))
	# v0.94.0: `require_hiring_role`, not `require_hr_role`. See `HIRING_ROLES` —
	# the foreman standing with the new hire is the back office on this farm, and
	# `require_company_scope` two lines down still holds them to their own entity.
	actor = require_hiring_role()

	employee_name = as_str(args, "employee_name", required=True)
	if " " not in employee_name:
		raise ToolError(
			f"employee_name is {employee_name!r}. An I-9, a payroll register and a dispatch board "
			"all name the same person, and a record carrying one word names nobody findable. "
			"Nothing was created."
		)
	company = require_company_scope(actor, resolve_company(as_str(args, "company"), required=True))

	# first/last default off the full name rather than being demanded again. The
	# first token and the last token is what a form filled in by hand produces,
	# and a caller who wants something else passes it.
	tokens = [part for part in employee_name.replace(",", " ").split() if part]
	payload = {
		"employee_name": employee_name,
		"company": company,
		"first_name": as_str(args, "first_name") or (tokens[0] if tokens else employee_name),
		"last_name": as_str(args, "last_name") or (tokens[-1] if len(tokens) > 1 else ""),
		"date_of_joining": _clean("date_of_joining", args.get("date_of_joining")) or frappe.utils.today(),
		"status": _clean("status", args.get("status") or DEFAULT_STATUS, "status"),
	}
	optional = (
		# Optional and never derived. `first_name` and `last_name` above fall
		# back to the first and last tokens of the full name, which is what a
		# hand-filled form produces; guessing a middle name out of the tokens
		# between them would put "de la" in somebody's Legal Middle Name box on
		# a federal form. It arrives from the licence barcode or not at all.
		"middle_name",
		"date_of_birth",
		"gender",
		"department",
		"designation",
		"employment_type",
		# v0.54.0. On `WRITABLE` beside the three above it, and it has to be here
		# too — that tuple is what `_reject_unknown` accepts, and THIS one is what
		# actually gets written. A field on the first and not the second is one
		# the tool takes without complaint and silently drops.
		"branch",
		# v0.68.1, and on this list for the same reason `branch` is. A hire whose
		# supervisor is known on the day is a hire whose first escalation has
		# somewhere to go; collecting it a week later in the Desk is how a farm
		# ends up with `dispatch.escalate_farm_task` refusing on half the crew.
		# No cycle is possible from here — the record does not exist yet, so
		# nothing can already report to it — which is why only `update_employee`
		# walks the chain.
		"reports_to",
		"personal_email",
		"cell_number",
		# v0.62.0. On `WRITABLE` and therefore here, for the reason the comment
		# above `branch` gives: a field on the first tuple and not on this one is
		# a field the tool accepts without complaint and silently drops.
		"current_address",
		"person_to_be_contacted",
		"emergency_phone_number",
		*COMPLIANCE_FIELDS,
	)
	for key in optional:
		value = _clean(key, args.get(key))
		if value:
			payload[key] = value

	# The three fields THIS APP marks mandatory get a starting value rather than a
	# refusal — see the module docstring. Applied after the loop above, so anything
	# the caller passed has already been cleaned into `payload` and wins.
	payload.update(_compliance_defaults(payload, company))

	user_id = _clean("user_id", args.get("user_id"), "user_id")
	linkage = None
	if user_id:
		_require_free_user(user_id)
		linkage = _require_enrolled(user_id, args)
		payload["user_id"] = user_id

	_refuse_a_second_record(employee_name, company, user_id, args)

	writable, absent = _supported(payload)
	gaps = _mandatory_gaps(writable)
	if gaps:
		raise ToolError(_mandatory_message(gaps))
	name = _insert(writable)

	data = {
		"employee": name,
		"employee_name": employee_name,
		"company": company,
		"actor": actor,
		"fields_set": {key: value for key, value in writable.items() if key != "doctype" and value},
		"defaults_applied": sorted(
			key
			for key in ("first_name", "last_name", "date_of_joining", "status", *COMPLIANCE_FIELDS)
			if not str(args.get(key) or "").strip() and writable.get(key)
		),
		"fields_not_on_this_site": absent,
		"linkage": linkage,
		"note": (
			"This is an identity and assignment record only. Payroll, tax and banking fields are "
			"refused by name — they have forms, approvals and retention rules this app knows "
			"nothing about."
			+ _compliance_note({key: writable.get(key) for key in COMPLIANCE_FIELDS if key in writable}, args)
			+ (
				""
				if not absent
				else f" This site's Employee doctype has no {', '.join(absent)}, so "
				"those values were NOT written."
			)
		),
		"next_step": (
			"generate_mobile_login_qr hands the credential over."
			if linkage and linkage.get("farm_ops_ready")
			else "create_mobile_user gives this person a scoped login, then "
			"link_employee_to_user connects the two."
		),
	}
	return ToolResult(
		data=data,
		summary=f"created Employee {name} — {employee_name} at {company}"
		+ (f", linked to {user_id}" if user_id else ""),
		docstatus_delta="none → 0 (Employee created)",
	)


def _compliance_defaults(payload: dict, company: str) -> dict:
	"""Starting values for the fields this app made mandatory, for the ones still empty.

	Read off `payload` rather than `args`, so what the CALLER passed has already
	been through `_clean` and validated against this site's own Select options
	before it counts as supplied. A field this site's Employee does not carry gets
	nothing — `_supported` would drop it a moment later, and a default that is
	silently discarded is worse than no default.

	Deliberately NOT conditional on the field being `reqd` here. An operator who
	cleared the flag has said "do not refuse for want of this", not "leave it
	blank": `Pending` and `Missing` are the true state of a new hire either way,
	and the compliance backlog counts a null as an unanswered question.
	"""
	defaults = {}
	for key in COMPLIANCE_FIELDS:
		if str(payload.get(key) or "").strip() or not compat.has_field(EMPLOYEE, key):
			continue
		value = _jurisdiction_for(company) if key == "jurisdiction" else COMPLIANCE_DEFAULTS.get(key, "")
		if not value:
			continue
		# Through `_clean` as well, so a site that edited the Select options gets a
		# refusal naming its own choices rather than a row carrying a value the
		# doctype does not offer.
		defaults[key] = _clean(key, value)
	return defaults


def _jurisdiction_for(company: str) -> str:
	"""The wage-law state to start this person on, off the hiring entity's own address.

	A STARTING VALUE, NOT A FINDING. Wage law follows where the work is PERFORMED,
	which nothing on a site knows at hire time — a crew that crossed the river to a
	Washington block is paid under RCW 49.46 that day and `jurisdiction` is the
	field that has to say so. The entity's own address is the closest honest guess,
	and `update_employee` is how it gets corrected.

	Falls back to `DEFAULT_JURISDICTION` rather than raising, because a blank here
	is precisely what the create was refusing on: a value an operator can see on
	the record and fix beats a hire that cannot happen.
	"""
	state = ""
	try:
		if (
			compat.doctype_exists("Address")
			and compat.doctype_exists("Dynamic Link")
			and compat.has_field("Address", "state")
		):
			addresses = (
				frappe.db.get_all(
					"Dynamic Link",
					filters={"link_doctype": "Company", "link_name": company, "parenttype": "Address"},
					pluck="parent",
					limit=5,
				)
				or []
			)
			for address in addresses:
				state = str(frappe.db.get_value("Address", address, "state") or "").strip()
				if state:
					break
	except Exception:  # pragma: no cover - a site without ERPNext's address schema
		state = ""

	if not state:
		return DEFAULT_JURISDICTION
	if len(state) == 2 and state.isalpha():
		return state.upper()
	return US_STATE_CODES.get(state.lower(), DEFAULT_JURISDICTION)


def _compliance_note(written: dict, args: dict) -> str:
	"""Say which compliance statuses this call INVENTED, and what moves each one.

	A record that quietly acquired an I-9 status is the record nobody goes back to
	fix, so the values that were not asked for are named in the result rather than
	left for somebody to notice in the Desk.
	"""
	defaulted = sorted(
		f"{key}={value}" for key, value in written.items() if value and not str(args.get(key) or "").strip()
	)
	if not defaulted:
		return ""
	return (
		f" This app marks {', '.join(COMPLIANCE_FIELDS)} mandatory on Employee and the call did not "
		f"supply {'them' if len(defaulted) > 1 else 'it'}, so it started this person at "
		f"{', '.join(defaulted)}. Those are starting values, not findings: create_i9_form and "
		"submit_i9_section_2 move i9_status, submit_w4 moves w4_status, and jurisdiction follows "
		"where the work is performed rather than where the entity sits — update_employee corrects "
		"it. Nothing else was assumed."
	)


def _refuse_a_second_record(employee_name: str, company: str, user_id: str, args: dict) -> None:
	"""One person, one Employee. See `newhire.py`, which has argued this since v0.17.1.

	Matched on name AND company, because the same person genuinely can be employed
	by two entities on a multi-entity site and that is a second record on purpose.
	`allow_duplicate_name=true` covers the real collision — two people called Ana
	Ramos — and names the existing record so the caller can tell which case they
	are in before they answer.
	"""
	if as_bool(args, "allow_duplicate_name", False):
		return
	existing = (
		frappe.db.get_all(
			EMPLOYEE,
			filters={"employee_name": employee_name, "company": company},
			fields=["name", "status", "user_id"]
			if compat.has_field(EMPLOYEE, "user_id")
			else ["name", "status"],
			limit=5,
		)
		or []
	)
	if not existing:
		return
	row = existing[0]
	raise ToolError(
		f"{employee_name} already has an Employee record at {company}: {row['name']} "
		f"(status {row.get('status') or 'unknown'}"
		+ (f", login {row['user_id']}" if row.get("user_id") else ", no login")
		+ "). Two Employee records for one person puts them on the dispatch board twice and in "
		"the payroll register once, and is far easier to make than to find. Use update_employee "
		+ (f"or link_employee_to_user on {row['name']}" if user_id else f"on {row['name']}")
		+ ", or pass allow_duplicate_name=true if this really is a different person with the "
		"same name. Nothing was created."
	)


def _insert(payload: dict) -> str:
	"""Insert the record, and turn a mandatory-field refusal into a sentence.

	`ignore_permissions` because the gate is `require_hr_role` plus the tool's own
	switch, both of which have already run — the same contract every other
	mutating tool in this app has with `dispatch`.

	`doctype` is added HERE rather than carried in the payload, because the payload
	has been through `_supported`, which keeps only keys that are fields on this
	site's Employee — and `doctype` is not one.
	"""
	doc = frappe.get_doc({**payload, "doctype": EMPLOYEE})
	doc.flags.ignore_permissions = True
	try:
		doc.insert(ignore_permissions=True)
	except Exception as exc:
		gaps = _mandatory_gaps(payload)
		if gaps and _is_mandatory_error(exc):
			raise ToolError(_mandatory_message(gaps)) from None
		raise
	return doc.name


def _mandatory_message(gaps: list) -> str:
	"""What to say when this site wants a field the call did not supply.

	It used to say "this site's Frappe HR marks …", which was wrong about whose
	decision it was whenever a gap was one of `COMPLIANCE_FIELDS` — those are
	erpnext_mcp's own Custom Fields, installed `reqd=True` by
	`compliance_fields.py`, and telling a caller to take it up with their operator
	sent them to argue with this app. Those three are defaulted now rather than
	refused; the sentence is kept honest anyway, because a site can end up with one
	of them required and unwritable in ways this file should not have to predict.
	"""
	ours = [gap for gap in gaps if gap in COMPLIANCE_FIELDS]
	theirs = [gap for gap in gaps if gap not in COMPLIANCE_FIELDS]
	whose = (
		f"this site's Frappe HR marks {', '.join(theirs)} mandatory on Employee"
		if theirs and not ours
		else f"erpnext_mcp installs {', '.join(ours)} on Employee as mandatory"
		if ours and not theirs
		else f"this site's Frappe HR marks {', '.join(theirs)} mandatory on Employee and "
		f"erpnext_mcp installs {', '.join(ours)} the same way"
	)
	return (
		f"{whose}, and the call did not supply "
		+ ("it" if len(gaps) == 1 else "them")
		+ ". Pass "
		+ ("the value" if len(gaps) == 1 else "the values")
		+ ", or clear the `reqd` flag in the Desk. Nothing was created."
	)


def _is_mandatory_error(exc: Exception) -> bool:
	"""Whether a failed insert failed for want of a required field.

	Matched on the exception's NAME rather than by importing `frappe.MandatoryError`,
	which does not exist on every version this app supports, and on the message as
	a fallback for a controller that raises a plain ValidationError instead.
	"""
	if type(exc).__name__ in ("MandatoryError", "MandatoryFieldError"):
		return True
	return "mandatory" in str(exc).lower()


# ── 2. update_employee ──────────────────────────────────────────────────────
def update_employee(args: dict) -> ToolResult:
	"""Change the identity and assignment fields on an Employee that already exists.

	STILL `require_hr_role`, DELIBERATELY, WHILE `create_employee` MOVED. Bringing
	somebody onto the farm and editing the record of somebody already on it are
	two different questions: the first is field work with a compliance record
	behind it, the second is a personnel register that names an existing person's
	department, supervisor, birth date and login. v0.94.0 widened the first and
	left this exactly where it was. `reactivate_employee` below is the one narrow
	exception, and it is narrow precisely so this gate can stay.
	"""
	compat.require_doctype(EMPLOYEE, "It comes with the Frappe HR (hrms) app.")
	_reject_unknown(
		args,
		WRITABLE,
		reserved=("name", "employee", "allow_unenrolled_user", "replace_user", "allow_duplicate_name"),
	)
	return _apply_employee_update(args, require_hr_role())


def reactivate_employee(args: dict) -> ToolResult:
	"""Put a returning worker back on the payroll: Active, joined today.

	STEP 1b OF A HIRE, NOT AN EDIT OF THE REGISTER, and that is the whole reason
	it is a separate tool with a separate gate. A worker who left in November and
	is standing in the yard in June is one Employee record with a status — not two
	records with one history between them — so the foreman who would have been
	allowed to `create_employee` a stranger must not be refused for the person
	they hired last season. Refusing here would push the field toward the worse
	record: a duplicate Employee, two I-9s, and a tenure history that starts over.

	IT WRITES EXACTLY TWO FIELDS AND NEITHER IS A CALLER'S CHOICE. `status` is
	Active and `date_of_joining` is today, both fixed in code below rather than
	read from `args`, so widening this gate does not widen `update_employee` by
	the back door — a caller holding only `HIRING_ROLES` cannot reach the
	department, the supervisor, the birth date or the login through this door,
	because there is no argument here that carries them.

	TODAY IS NOT AN ARGUMENT, for the same reason it is not one on the handset:
	8 U.S.C. §1324a's three-business-day clock counts from the day this person
	started THIS time, and the phone in the yard knows exactly one true answer to
	that. A backdated rehire is a correction, and a correction is `update_employee`
	in the Desk by somebody who can see what they are correcting.

	THE ORIGINAL HIRE DATE IS OVERWRITTEN AND IS NOT LOST — the `changed` list
	this returns carries the before-value, and that lands in the MCP Action Log
	row the caller writes.
	"""
	compat.require_doctype(EMPLOYEE, "It comes with the Frappe HR (hrms) app.")
	_reject_unknown(args, (), reserved=("name", "employee"))
	actor = require_hiring_role()
	name = resolve_employee(as_str(args, "name") or as_str(args, "employee", required=True))
	return _apply_employee_update(
		{"employee": name, "status": "Active", "date_of_joining": frappe.utils.today()},
		actor,
	)


def _apply_employee_update(args: dict, actor: str) -> ToolResult:
	"""The shared body of `update_employee` and `reactivate_employee`.

	ONE WRITE PATH, TWO GATES. The gate is the caller's argument rather than
	something read in here, so the two tools cannot drift into two different
	notions of what a change to an Employee costs — the before/after `changed`
	report, the company scope, the enrolment check on `user_id` and the supervisor
	cycle refusal are all written once and run for both.
	"""
	name = resolve_employee(as_str(args, "name") or as_str(args, "employee", required=True))
	current = (
		frappe.db.get_value(
			EMPLOYEE,
			name,
			compat.existing_fields(EMPLOYEE, ["employee_name", "company", "user_id", *WRITABLE]),
			as_dict=True,
		)
		or {}
	)
	require_company_scope(actor, str(current.get("company") or ""))

	wanted = {key: args[key] for key in WRITABLE if key in args}
	if not wanted:
		raise ToolError(
			"update_employee was given nothing to change. Pass at least one of: "
			f"{', '.join(WRITABLE)}. Nothing was changed."
		)

	if "company" in wanted:
		wanted["company"] = require_company_scope(
			actor, resolve_company(as_str(wanted, "company"), required=True)
		)
	if "employee_name" in wanted and " " not in str(wanted["employee_name"] or "").strip():
		raise ToolError(
			f"employee_name is {wanted['employee_name']!r}, and a record carrying one word names "
			"nobody findable. Nothing was changed."
		)

	linkage = None
	values = {}
	for key, raw in wanted.items():
		if key == "company":
			values[key] = wanted[key]
			continue
		value = _clean(key, raw, key)
		if key == "user_id" and value:
			_require_free_user(value, employee=name)
			existing_login = str(current.get("user_id") or "")
			if existing_login and existing_login != value and not as_bool(args, "replace_user", False):
				raise ToolError(
					f"{name} is already linked to {existing_login}. Re-pointing an Employee at a "
					"different login moves that person's whole task history to another account, "
					"which is a decision rather than a retry — pass replace_user=true to say so. "
					"Nothing was changed."
				)
			linkage = _require_enrolled(value, args)
		if key == "reports_to" and value:
			_refuse_supervisor_cycle(name, value)
		values[key] = value

	writable, absent = _supported(values)
	changed, unchanged = [], []
	for key, value in writable.items():
		before = current.get(key)
		if str(before or "") == str(value or ""):
			unchanged.append(key)
			continue
		changed.append({"field": key, "from": before, "to": value or None})

	if changed:
		doc = frappe.get_doc(EMPLOYEE, name)
		for entry in changed:
			doc.set(entry["field"], entry["to"])
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)

	data = {
		"employee": name,
		"employee_name": current.get("employee_name"),
		"company": writable.get("company") or current.get("company"),
		"actor": actor,
		"changed": changed,
		"unchanged": sorted(unchanged),
		"fields_not_on_this_site": absent,
		"linkage": linkage,
		"note": (
			f"Only the {len(WRITABLE)} identity, assignment, contact and compliance-status "
			"fields are writable here. Payroll, tax "
			"and banking fields are refused by name and belong in the Desk, where the HR "
			"module's own validation runs."
		),
	}
	return ToolResult(
		data=data,
		summary=(
			f"updated Employee {name}: "
			+ (", ".join(entry["field"] for entry in changed) if changed else "nothing to change")
		),
		docstatus_delta="0 → 0 (Employee amended)" if changed else "none",
	)


#: How far `_refuse_supervisor_cycle` climbs before it stops asking. Deep enough
#: for any real org chart — a farm's is three or four — and finite because the
#: chain it walks may ALREADY contain a cycle put there before this tool existed,
#: in which case the walk must end rather than the request hang.
SUPERVISOR_CHAIN_CAP = 50


def _refuse_supervisor_cycle(employee: str, supervisor: str) -> None:
	"""Refuse a `reports_to` that would make somebody their own manager.

	THE ONLY SELF-REFERENTIAL LINK THIS APP WRITES, so it is the only one that
	can close a loop. `shadow_log.walk` and `dispatch` both READ this chain, and
	both are written to survive a cycle by reporting one — which is right for a
	reader and is not a reason to let a writer create one. The refusal names both
	ends and the path between them, because "A reports to B reports to A" is a
	sentence somebody can act on and "invalid value" is not.

	A chain that is ALREADY broken is not made the caller's problem: the walk
	stops at `SUPERVISOR_CHAIN_CAP` and lets the write through, because refusing
	an unrelated correction over a loop somebody else left in the Desk would make
	this tool the one thing that cannot fix it.
	"""
	if not compat.has_field(EMPLOYEE, "reports_to"):
		return
	if supervisor == employee:
		name = frappe.db.get_value(EMPLOYEE, employee, "employee_name") or employee
		raise ToolError(
			f"{name} cannot report to themselves. An escalation with nowhere to go looks "
			"exactly like one that was handled. Nothing was changed."
		)

	path = [supervisor]
	seen = {supervisor}
	current = supervisor
	for _ in range(SUPERVISOR_CHAIN_CAP):
		nxt = str(frappe.db.get_value(EMPLOYEE, current, "reports_to") or "").strip()
		if not nxt:
			return
		if nxt == employee:
			path.append(employee)
			raise ToolError(
				f"setting reports_to to {supervisor} would close a loop: "
				f"{' → '.join([employee, *path])}. Everybody in a cycle is their own "
				"supervisor, so an escalation walks it forever and a review ladder has no "
				"top. Point one of them at somebody outside the loop first. Nothing was "
				"changed."
			)
		if nxt in seen:
			# A loop that does not include this employee, and therefore one this
			# call did not create. Reported nowhere and refused nothing — see the
			# docstring on why an unrelated correction is not held hostage to it.
			return
		seen.add(nxt)
		path.append(nxt)
		current = nxt


# ── 3. link_employee_to_user ────────────────────────────────────────────────
def link_employee_to_user(args: dict) -> ToolResult:
	"""Point one Employee at one login, and report whether the phone will now work."""
	compat.require_doctype(EMPLOYEE, "It comes with the Frappe HR (hrms) app.")
	actor = require_hr_role()

	if not compat.has_field(EMPLOYEE, "user_id"):
		raise ToolError(
			"this site's Employee doctype has no `user_id` field, so there is nothing to link a "
			"login to. That field is how every Farm Ops method resolves a phone to a person; "
			"without it the mobile API cannot scope anything. Nothing was changed."
		)

	employee = resolve_employee(
		as_str(args, "employee_name") or as_str(args, "employee") or as_str(args, "name", required=True)
	)
	user = as_str(args, "user_id", required=True).strip().lower()
	if not frappe.db.exists(USER, user):
		raise ToolError(
			f"no User {user!r} on this site. A login has to exist before an Employee can point at "
			"it — create_mobile_user makes one with a role, entity scoping and a credential. "
			"Nothing was changed."
		)

	row = (
		frappe.db.get_value(
			EMPLOYEE, employee, ["employee_name", "company", "user_id", "status"], as_dict=True
		)
		or {}
	)
	require_company_scope(actor, str(row.get("company") or ""))

	already = str(row.get("user_id") or "")
	if already == user:
		# IDEMPOTENT, AND SAYING SO. An orchestrator re-run must not look like a
		# failure, and a caller who asked for a state that already holds got what
		# they asked for.
		state = linkage_state(user)
		return ToolResult(
			data={
				"employee": employee,
				"employee_name": row.get("employee_name"),
				"company": row.get("company"),
				"user_id": user,
				"action": "already linked",
				"actor": actor,
				"linkage": state,
				"note": _readiness_note(state, row),
			},
			summary=f"{employee} was already linked to {user}",
			docstatus_delta="none",
		)

	_require_free_user(user, employee=employee)
	if already and not as_bool(args, "replace_user", False):
		raise ToolError(
			f"{employee} ({row.get('employee_name')}) is already linked to {already}. Re-pointing "
			"an Employee at a different login moves that person's whole task history to another "
			"account, which is a decision rather than a retry — pass replace_user=true to say so. "
			"Nothing was changed."
		)
	state = _require_enrolled(user, args)

	doc = frappe.get_doc(EMPLOYEE, employee)
	doc.user_id = user
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	state = linkage_state(user)
	return ToolResult(
		data={
			"employee": employee,
			"employee_name": row.get("employee_name"),
			"company": row.get("company"),
			"user_id": user,
			"action": "relinked" if already else "linked",
			"previous_user_id": already or None,
			"actor": actor,
			"linkage": state,
			"note": _readiness_note(state, row),
		},
		summary=f"linked Employee {employee} ({row.get('employee_name')}) to {user}",
		docstatus_delta="0 → 0 (Employee amended)",
	)


# ── 4. get_employee ─────────────────────────────────────────────────────────
#: What `employee_detail` reads off the Employee row, in the order somebody
#: filling in a form would meet them. `employee_number` is here under its own
#: name and is re-emitted as `employee_id`, because that is what the app calls
#: it — see `_employee_identity` in `api/mobile.py`, which made the same rename
#: for the same reason.
DETAIL_FIELDS = (
	"employee_name",
	"first_name",
	"middle_name",
	"last_name",
	"employee_number",
	"status",
	"gender",
	"date_of_birth",
	"date_of_joining",
	"relieving_date",
	"company",
	"employment_type",
	"designation",
	"department",
	"personal_email",
	"cell_number",
	"user_id",
	*COMPLIANCE_FIELDS,
)

#: The doctype `link_badge_to_employee` writes, read here for `badge_id`. Named
#: rather than imported from `tools/bucket_log.py` because importing it would
#: make the personnel register depend on the bucket queue in the wrong
#: direction; the constant is asserted against that module's own in
#: `test_employee.py`, so the two cannot drift apart silently.
BADGE_MAP = "Bucket Log Badge Map"

#: The I-9 and W-4 records the wizard's later steps create, and the state each
#: reaches when that step is genuinely done. `I-9 Form.status` runs Draft →
#: Section 1 Complete → Awaiting Verification → Complete; `W-4 Form.status` runs
#: Draft → Active → Superseded.
I9_FORM = "I-9 Form"
W4_FORM = "W-4 Form"
I9_COMPLETE = "Complete"
W4_ACTIVE = "Active"

#: The column values a completed record is allowed to WRITE OVER, and nothing
#: else. Empty is a record that predates the Custom Field; `Pending` and
#: `Missing` are the hire-time defaults `COMPLIANCE_DEFAULTS` sets, which is to
#: say "nobody has answered this yet" — and a live Complete I-9 or Active W-4 is
#: the answer. Every OTHER value is somebody's deliberate statement and is left
#: alone: `Expired` above all, which is the one case where a returning worker
#: must be re-verified before working and where a stale `Complete` from an
#: earlier season is exactly the wrong thing to trust.
I9_UNANSWERED = ("", "Pending")
W4_UNANSWERED = ("", "Missing")

#: What the column reads once the record has answered it. Options of the Selects
#: `compliance_fields.py` installs, and the two values `EmployeeDetail`
#: (`OnboardingModels.swift:319`) treats as "nothing to collect today".
I9_ANSWERED = "Verified"
W4_ANSWERED = "On-File"


def employee_detail(name: str) -> dict:
	"""One Employee, with the compliance state the onboarding wizard skips steps on.

	NO ROLE GATE AND NO SCOPE CHECK. This is the read and the shaping, split out
	so `get_employee` below and `api/mobile.get_employee` can share one answer
	while gating it differently — the tool wants `require_hr_role`, and the phone
	additionally lets somebody read their OWN record without it. A helper that
	carried a gate could not serve both, and a second copy of the shaping would
	be a second contract for iOS's `EmployeeDetail` to drift away from. Both
	callers gate BEFORE they call this; nothing else in the app may call it.

	THE TWO COMPLIANCE COLUMNS ARE STALE BY CONSTRUCTION, AND THIS RECONCILES
	THEM. `i9_status` and `w4_status` are Custom Fields `compliance_fields.py`
	installs on Employee; `create_employee` starts a hire at Pending/Missing and
	NOTHING IN THIS APP MOVES THEM AFTERWARDS. `submit_i9_section_2` sets
	`I-9 Form.status = "Complete"` and `submit_w4` sets `W-4 Form.status =
	"Active"`, each on its own doctype, and neither writes back to the Employee
	row. So a picker whose I-9 was verified last June still reads
	`i9_status = "Pending"` in the column — and `EmployeeDetail.satisfiedSteps`
	(`OnboardingModels.swift:352`) branches on exactly that column, so a wrapper
	that handed it over raw would walk a fully documented worker through an I-9
	and a W-4 they already have. That is the precise failure this endpoint exists
	to prevent, so the answer would have been worse than no answer.

	The record wins over an UNANSWERED column and over nothing else. A live
	Complete I-9 fills a column reading empty or `Pending`; every other value —
	`Expired` above all — is somebody's deliberate statement and stands, because
	an expired I-9 is the one case where a returning worker must be re-verified
	before working. `W4_UNANSWERED` says the same for `Missing` against
	`Requires-Update`.

	NOTHING IS HIDDEN BY THE RECONCILIATION. `i9_status_recorded` and
	`w4_status_recorded` carry the column exactly as stored, `i9` and `w4` carry
	the record that answered it, and `reconciled` names the fields the record
	moved. An operator reading this and an alert rule reading the column will
	disagree, and both need to be able to see why.

	THE REAL FIX IS ELSEWHERE AND IS NOT THIS. `submit_i9_section_2` and
	`submit_w4` should write the column through when they close a form. Doing it
	here as well would still be right for the records that already exist — every
	season already worked was filed before any such release — but a read that
	repairs its own inputs is a patch, and it is documented as one.

	THE COMPLETENESS FLAGS ARE NOT COMPUTED HERE. `EmployeeDetail.needsI9`,
	`.needsW4` and `.needsBadge` are three lines of Swift over the fields below,
	and a server that also decided which step to skip would be a second rule for
	the skipping to drift away from — the same argument `api/mobile.py` makes
	about the dispatch rules. What this owes the wizard is every fact those three
	properties read, which is what it returns.
	"""
	row = (
		frappe.db.get_value(
			EMPLOYEE, name, compat.existing_fields(EMPLOYEE, list(DETAIL_FIELDS)), as_dict=True
		)
		or {}
	)
	detail = {"name": name, "employee_id": str(row.get("employee_number") or "") or None}
	for field in DETAIL_FIELDS:
		if field == "employee_number":
			continue
		detail[field] = row.get(field) if field in row else None

	i9 = _latest(I9_FORM, name, ("status", "hire_date", "verification_date"), "hire_date")
	w4 = _latest(W4_FORM, name, ("status", "tax_year", "effective_date"), "effective_date")
	detail["i9"] = i9
	detail["w4"] = w4
	detail["badge_id"] = _active_badge(name)
	detail["i9_on_file"] = bool(i9 and i9.get("status") == I9_COMPLETE)
	detail["w4_on_file"] = bool(w4 and w4.get("status") == W4_ACTIVE)

	detail["i9_status_recorded"] = detail.get("i9_status")
	detail["w4_status_recorded"] = detail.get("w4_status")
	reconciled = []
	for field, answered, unanswered, on_file in (
		("i9_status", I9_ANSWERED, I9_UNANSWERED, detail["i9_on_file"]),
		("w4_status", W4_ANSWERED, W4_UNANSWERED, detail["w4_on_file"]),
	):
		if not on_file or not compat.has_field(EMPLOYEE, field):
			continue
		if str(detail.get(field) or "").strip() not in unanswered:
			continue
		# The site's options are the arbiter here as everywhere else in this file:
		# an operator who edited the Select gets their own value or none, never a
		# value invented from this module's idea of what the options are.
		options = select_options(EMPLOYEE, field)
		if options and answered not in options:
			continue
		detail[field] = answered
		reconciled.append(field)
	detail["reconciled"] = reconciled

	# v0.98.0. WHETHER THIS PERSON IS UNDER EIGHTEEN, DERIVED AND NEVER STORED.
	# `minors.py` argues the whole of it; the short version is that a stored flag
	# is correct on the day it is ticked and wrong every day afterwards, and the
	# direction it goes wrong in is the one that permits more work. Computed
	# against TODAY because that is the day a roster question is being asked
	# about — a caller reconstructing a past shift asks `minors.describe` with the
	# shift's own date rather than reading this.
	#
	# THREE-VALUED. `is_minor` is None where no date of birth is on file, which is
	# not the same as False, and `date_of_birth_recorded` is what tells the two
	# apart. iOS renders the purple badge on True and nothing on either of the
	# others, which is right; a foreman reading the record still sees the gap.
	detail.update(minors.describe(detail.get("date_of_birth"), frappe.utils.today()))

	# v0.106.0. WHAT THIS PERSON'S ROLES SAY THEY MAY DO, so a picker can stop
	# offering an approval task to somebody the server is about to refuse. Read
	# through `user_id` — a role hangs off a LOGIN, not off an Employee — and
	# `has_login` distinguishes "no roles" from "no account", which for most of a
	# picking crew is the true answer and a different sentence to show a foreman.
	#
	# NOT `designation`. That is a job title beside this and not a weaker version
	# of it: a Checker is a designation and carries no role at all, and a Foreman
	# is a role that several designations hold. Both are returned; neither is
	# derived from the other. See `roles.py`'s job-title table.
	detail["capability"] = roles.capability_of(detail.get("user_id"))
	# Flattened as well as nested, because `EmployeeDetail` decodes a flat struct
	# and a nested object it does not declare is a key it silently drops.
	detail["mobile_roles"] = detail["capability"]["mobile_roles"]
	detail["can_dispatch"] = detail["capability"]["can_dispatch"]
	return detail


def _latest(doctype: str, employee: str, fields: tuple, order_field: str) -> dict | None:
	"""The most recent record of `doctype` for this employee, or None.

	MOST RECENT BY THE DOCTYPE'S OWN DATE, falling back to `creation` where this
	site's copy has no such column. A returning picker has an I-9 from every
	season they worked and a W-4 from every year they filed one, and the one the
	wizard branches on is the current one — ordering by `modified` instead would
	put a record somebody merely corrected in front of the one that supersedes it.

	Returns None where the doctype is not installed at all, which is a site that
	never migrated this app rather than an error: the columns above still answer
	and the caller sees a null rather than a traceback.
	"""
	if not compat.doctype_exists(doctype):
		return None
	wanted = compat.existing_fields(doctype, ["name", *fields])
	order_by = f"{order_field} desc" if compat.has_field(doctype, order_field) else "creation desc"
	try:
		rows = frappe.db.get_all(
			doctype, filters={"employee": employee}, fields=wanted, order_by=order_by, limit_page_length=1
		)
	except Exception:  # pragma: no cover - a site whose copy has no `employee` column
		return None
	return dict(rows[0]) if rows else None


def _active_badge(employee: str) -> str | None:
	"""The scanning badge mapped to this person, or None.

	`badge_id` IS NOT A FIELD ON EMPLOYEE. `link_badge_to_employee` writes a
	`Bucket Log Badge Map` row — one badge, one employee, one company, and an
	`active` flag that a reissued badge clears rather than deletes. So the answer
	is a lookup rather than a column, and an INACTIVE mapping is not it: a badge
	that was handed back is exactly the badge the wizard's step 5 needs to issue
	again.
	"""
	if not compat.doctype_exists(BADGE_MAP):
		return None
	rows = (
		frappe.db.get_all(
			BADGE_MAP,
			filters={"employee": employee, "active": 1},
			fields=["badge_id"],
			order_by="modified desc",
			limit_page_length=1,
		)
		or []
	)
	if not rows:
		return None
	return str(rows[0].get("badge_id") or "") or None


def get_employee(args: dict) -> ToolResult:
	"""One Employee record, with the onboarding state a returning worker arrives with."""
	compat.require_doctype(EMPLOYEE, "It comes with the Frappe HR (hrms) app.")
	actor = require_hr_role()

	name = resolve_employee(as_str(args, "employee") or as_str(args, "name", required=True))
	detail = employee_detail(name)
	require_company_scope(actor, str(detail.get("company") or ""))

	return ToolResult(
		data={
			**detail,
			"employee": name,
			"actor": actor,
			"note": (
				"`i9_status` and `w4_status` are RECONCILED: nothing in this app writes those "
				"columns after the hire, so a live Complete I-9 or Active W-4 fills a column "
				"still reading its hire-time default. `i9_status_recorded` and "
				"`w4_status_recorded` are the columns as stored, `i9` and `w4` are the records "
				"that answered them, and `reconciled` names what moved. Expired and "
				"Requires-Update are deliberate statements and are never written over. "
				"update_employee is what makes the stored column agree."
			),
		},
		summary=(
			f"{name} — {detail.get('employee_name')} at {detail.get('company')}, "
			f"{detail.get('status')}; I-9 "
			+ ((detail["i9"] or {}).get("status") or "none")
			+ ", W-4 "
			+ ((detail["w4"] or {}).get("status") or "none")
			+ ", badge "
			+ (detail["badge_id"] or "none")
		),
		docstatus_delta="none",
	)


#: What an onboarding attachment is allowed to be. The same list
#: `api/files._ALLOWED_EXTENSIONS` will accept on the way up, minus nothing:
#: a file that could not be staged cannot arrive here, and a list that differed
#: would be a refusal at the second of two calls, after the bytes had already
#: crossed the link.
ONBOARDING_EXTENSIONS = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".pdf")

#: What the phone is filing, and it is a LABEL rather than a field name. The
#: wizard collects six kinds of evidence and every one of them lands on the
#: Employee as an ordinary private attachment; the kind is recorded in the
#: File's name and in the audit row so an inspection can tell a List B photo
#: from a Section 1 signature without opening either.
ONBOARDING_KINDS = (
	"i9_section_1_signature",
	"i9_section_2_document",
	"i9_section_3_signature",
	"i9_list_a_document",
	"i9_list_b_document",
	"i9_list_c_document",
	"w4_signed",
	"identity_document",
	"profile_photo",
	"other",
)

#: What a badge photograph may be. A SUBSET of `ONBOARDING_EXTENSIONS`: a PDF is
#: a perfectly good scan of a signed W-4 and is not a face, and `Employee.image`
#: is rendered in an `<img>` by Desk, by the badge template and by the phone.
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp")


def attach_employee_document(args: dict) -> ToolResult:
	"""File an already-uploaded File against an Employee, privately.

	THE SECOND HALF OF THE ONBOARDING UPLOAD, and the reason this exists at all.
	`api/files.finalize_staged_file` commits evidence UNATTACHED on purpose — its
	module docstring is emphatic about it, because forwarding an attachment target
	from a phone would let a field worker hang a file off any document on the
	site. So the staged path produces a private File and nothing that points at it.
	For task evidence that is fine: `complete_task_via_mobile` takes the token and
	the tool it delegates to does the attaching. For the onboarding wizard's six
	photographs and signatures there was no such call, and the app was reaching
	Frappe's own `/api/method/upload_file` instead — a path the funnel strips the
	credential from, which answered 200 with a login page and lost every one of
	them silently. This is the narrow, scoped equivalent.

	NO BYTES CROSS THIS BOUNDARY. It takes a `file_token` — the File docname
	`finalize_staged_file` handed back — or a `file_url` for a File already on the
	site, exactly as `i9.attach_signed_i9` does and for the same reason: a second
	upload path would have its own size limit and its own way of failing halfway
	up a hill.

	THE FILE IS MADE PRIVATE ON THE WAY IN, whatever it was, and through the
	document rather than through `db.set_value`. `is_private` is not a flag on a
	row — flipping it MOVES the bytes between `public/files` and `private/files`
	and rewrites `file_url`, and Frappe's File controller is what does the moving.
	A photograph of somebody's passport served from a public URL is a breach
	nobody has to guess a password for.

	A FILE ALREADY ATTACHED SOMEWHERE ELSE IS REFUSED. Re-pointing an existing
	attachment would silently take evidence off the record it was filed against,
	and the two records are federal ones. A file already on THIS employee is a
	no-op, because a phone that retried a call it did not hear the answer to must
	not produce a second copy.

	THE ROLE GATE IS THE CALLER'S TO RUN, not this function's — `require_hr_role`
	and the entity scoping both belong to whoever is calling, and the mobile
	wrapper runs them before it gets here. Every other tool in this file does the
	same.
	"""
	employee = resolve_employee(as_str(args, "employee", required=True))
	kind = (as_str(args, "document_kind") or "other").strip().lower()
	if kind not in ONBOARDING_KINDS:
		raise ToolError(
			f"document_kind must be one of: {', '.join(ONBOARDING_KINDS)}. Got {kind!r}. "
			f"It is a label on the audit row, not a field name — 'other' is the honest "
			f"answer for anything not on the list. Nothing was changed."
		)

	reference = as_str(args, "file_token") or as_str(args, "file") or as_str(args, "file_url")
	if not reference:
		raise ToolError(
			"file_token is required — it is what finalize_staged_file hands back. Upload the "
			"photograph with stage_file_chunk and finalize_staged_file first; this call only "
			"names it. Nothing was changed."
		)

	docname = ""
	if frappe.db.exists("File", reference):
		docname = reference
	elif reference.startswith("/") or reference.startswith("http"):
		docname = str(frappe.db.get_value("File", {"file_url": reference}, "name") or "")
	if not docname:
		raise ToolError(
			f"no File called {reference!r} on this site. Upload the file first — "
			f"stage_file_chunk then finalize_staged_file — and pass the file_token that "
			f"returns. Nothing was changed."
		)

	row = (
		frappe.db.get_value(
			"File",
			docname,
			["file_name", "file_url", "attached_to_doctype", "attached_to_name"],
			as_dict=True,
		)
		or {}
	)
	stored_name = str(row.get("file_name") or "")
	extension = ("." + stored_name.rsplit(".", 1)[-1].lower()) if "." in stored_name else ""
	if extension not in ONBOARDING_EXTENSIONS:
		raise ToolError(
			f"{stored_name or reference!r} is a {extension or 'file with no extension'} and an "
			f"onboarding document is a photograph or a scan: "
			f"{', '.join(ONBOARDING_EXTENSIONS)}. Nothing was changed."
		)

	already = str(row.get("attached_to_doctype") or ""), str(row.get("attached_to_name") or "")
	if already == (EMPLOYEE, employee):
		# The retry case. A phone whose call timed out does not know whether it
		# landed, and the only safe answer is the one it would have got.
		# THE CROSS-REFERENCE IS RETRIED TOO, and this branch is the reason it has
		# to be. v0.136.0. `link_document_copy` never raises and answers "" when
		# there is no open I-9 yet — which is a real ordering on a bad link, where
		# the photograph lands before the section it belongs to. If this early
		# return skipped the link, that form would never acquire the reference:
		# the file is attached, so every later attempt takes this path. Linking
		# here is idempotent (it writes the same URL to the same column) and is
		# what makes a retry converge instead of preserving the gap.
		stored_url = str(row.get("file_url") or "")
		data = {
			"employee": employee,
			"document_kind": kind,
			"file_token": docname,
			"file_url": stored_url,
			"file_name": stored_name,
			"is_private": True,
			"already_attached": True,
		}
		from . import i9 as i9_tool

		linked = i9_tool.link_document_copy(employee, kind, stored_url)
		if linked:
			data["i9_form"] = linked
		return ToolResult(
			data=data,
			summary=f"{stored_name} is already filed against {employee} — nothing changed",
			docstatus_delta="none",
		)
	if already[0] and already != (EMPLOYEE, employee):
		raise ToolError(
			f"File {docname} is already attached to {already[0]} {already[1]}. Moving it would "
			f"take that evidence off the record it was filed against. Upload the photograph "
			f"again and file the new one. Nothing was changed."
		)

	handle = frappe.get_doc("File", docname)
	handle.attached_to_doctype = EMPLOYEE
	handle.attached_to_name = employee
	handle.is_private = 1
	handle.flags.ignore_permissions = True
	handle.save()

	# The URL AFTER the move, not the one that was read a moment ago: a file that
	# has just changed directory has a different URL.
	stored_url = str(handle.get("file_url") or row.get("file_url") or "")

	# ── THE EXAMINED DOCUMENT ALSO GETS NAMED ON THE I-9 ────────────────
	#
	# v0.136.0, and it is the step that was missing rather than a new feature.
	# The wizard has photographed the List A / List B+C documents at the tailgate
	# for several releases and filed them here; the I-9 itself held a
	# `document_copies_stored` tickbox and no way to say WHICH copies. 8 CFR
	# 274a.2(b)(3) has an employer who keeps copies retain them WITH the form and
	# produce them with it, so the cross-reference is part of the record rather
	# than a convenience.
	#
	# IMPORTED HERE RATHER THAN AT MODULE SCOPE because `tools/i9` imports THIS
	# module — a top-level import would be a cycle. This is the only call that
	# needs it.
	#
	# BEST-EFFORT, AND THE ANSWER SAYS SO. `link_document_copy` never raises: the
	# photograph has already landed and the worker has put their passport away,
	# so a failed cross-reference must not read as a failed upload and send
	# somebody back to photograph it again. `i9_form` is the form it was linked
	# to, or absent where nothing was.
	from . import i9 as i9_tool

	linked = i9_tool.link_document_copy(employee, kind, stored_url)

	data = {
		"employee": employee,
		"document_kind": kind,
		"file_token": docname,
		"file_url": stored_url,
		"file_name": stored_name,
		"is_private": True,
		"already_attached": False,
	}
	summary = f"filed {stored_name} ({kind}) against {employee} as a private attachment"
	if linked:
		data["i9_form"] = linked
		summary += f" and referenced it on I-9 {linked}"
	return ToolResult(data=data, summary=summary, docstatus_delta="none")


def set_employee_photo(args: dict) -> ToolResult:
	"""MUTATING (default OFF). File a headshot against an Employee and make it
	the photograph on their record — the one a badge card prints.

	WHY THIS IS NOT JUST `attach_employee_document`. That call files evidence:
	the bytes land as a private File pointing at the Employee, and nothing on
	the Employee points back. That is exactly right for a List B photograph,
	which an inspector goes looking for, and exactly wrong for a headshot, which
	a badge template reads off `Employee.image` and finds empty. Every card
	printed for somebody onboarded through the wizard fell back to their
	initials for that reason. This is the same attach followed by the one field
	update that closes the loop.

	IT STAYS PRIVATE. `attach_employee_document` moves the file into
	`private/files` and this does not undo that: a workforce's faces served from
	a guessable public URL is a breach nobody needs a password for. Desk, the
	print template and the phone all read it through the permission check, which
	is the access this warrants.

	`db.set_value` RATHER THAN THE DOCUMENT, on purpose. `image` carries no side
	effects — the bytes were already moved by the attach — and saving the whole
	Employee would run its validation, which on a half-filled wizard record can
	refuse for a reason that has nothing to do with a photograph. A hire whose
	badge photo cannot be set because their designation is blank is not a
	trade worth making.

	A RETAKE SUPERSEDES RATHER THAN DELETES. The previous File stays attached to
	the Employee and `image` moves to the new one, so the record keeps what it
	was shown and the card prints what is current.

	THE ROLE GATE IS THE CALLER'S, as everywhere else in this file — the mobile
	wrapper runs `require_hr_role` and the entity scoping before it gets here.
	"""
	employee = resolve_employee(as_str(args, "employee", required=True))

	reference = as_str(args, "file_token") or as_str(args, "file") or as_str(args, "file_url")
	if not reference:
		raise ToolError(
			"file_token is required — it is what finalize_staged_file hands back. Upload the "
			"photograph with stage_file_chunk and finalize_staged_file first; this call only "
			"names it. Nothing was changed."
		)

	# Refuse a non-image BEFORE the attach, so a PDF sent by mistake does not
	# end up filed against the record with the field left unset.
	stored_name = ""
	if frappe.db.exists("File", reference):
		stored_name = str(frappe.db.get_value("File", reference, "file_name") or "")
	elif reference.startswith("/") or reference.startswith("http"):
		stored_name = str(frappe.db.get_value("File", {"file_url": reference}, "file_name") or "")
	if stored_name:
		extension = ("." + stored_name.rsplit(".", 1)[-1].lower()) if "." in stored_name else ""
		if extension not in PHOTO_EXTENSIONS:
			raise ToolError(
				f"{stored_name!r} is a {extension or 'file with no extension'} and a badge "
				f"photograph is an image: {', '.join(PHOTO_EXTENSIONS)}. Nothing was changed."
			)

	attached = attach_employee_document(
		{"employee": employee, "file_token": reference, "document_kind": "profile_photo"}
	)
	data = dict(attached.data or {})
	photo_url = str(data.get("file_url") or "")

	if not compat.has_field(EMPLOYEE, "image"):
		# The file IS filed; only the pointer could not be written. Say both,
		# because "photo set" would be a lie and "failed" would send somebody
		# looking for bytes that are on the record.
		return ToolResult(
			data={**data, "photo_url": photo_url, "image_set": False},
			summary=(
				f"filed {data.get('file_name')} against {employee}, but this site's Employee has "
				"no `image` field, so the badge card will still print initials"
			),
			docstatus_delta="none",
		)

	previous = str(frappe.db.get_value(EMPLOYEE, employee, "image") or "")
	if previous != photo_url:
		frappe.db.set_value(EMPLOYEE, employee, "image", photo_url)

	return ToolResult(
		data={
			**data,
			"photo_url": photo_url,
			"image_set": True,
			"previous_photo_url": previous or None,
			"replaced": bool(previous and previous != photo_url),
		},
		summary=(
			f"{employee}'s badge photograph is now {data.get('file_name')}"
			+ (" (replacing the previous one)" if previous and previous != photo_url else "")
		),
		docstatus_delta="none",
	)


def _readiness_note(state: dict, row: dict) -> str:
	"""Whether the phone will actually get a task list now, and what is missing if not.

	THIS IS THE SENTENCE THE RELEASE EXISTS FOR. "The link was written" is not the
	fact anybody wanted; "list_my_tasks will now answer for this account" is.
	"""
	if str(row.get("status") or "Active") != "Active":
		return (
			f"LINKED, BUT THIS EMPLOYEE IS {row.get('status')}. The mobile methods answer for "
			"Active employees; set status=Active with update_employee when they start."
		)
	if state.get("farm_ops_ready"):
		return (
			"list_my_tasks and the other ten mobile methods will now answer for this account: it "
			"holds "
			+ ", ".join(state.get("farm_ops_roles") or [])
			+ ", its Mobile Access Grant is Active, and the Employee it resolves to is this one."
		)
	if not state.get("farm_ops_roles"):
		return (
			"LINKED, BUT THE MOBILE METHODS WILL STILL REFUSE THIS ACCOUNT: it holds none of "
			+ ", ".join(sorted(_farm_ops_roles()))
			+ ". create_mobile_user grants one."
		)
	return (
		"LINKED, BUT THE MOBILE METHODS WILL STILL REFUSE THIS ACCOUNT: its Mobile Access Grant "
		f"is {state.get('grant_state') or 'missing'}, and the enrolment gate wants Active. "
		"create_mobile_user (update_existing=true) enrols it."
	)

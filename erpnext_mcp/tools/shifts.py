# SPDX-License-Identifier: MIT
"""Forming a crew, logging what happened to it, and signing the shift off.

v0.19.3. SIX TOOLS THAT ARE ONE WORKFLOW, and the workflow has exactly one actor.
`start_shift` forms the crew, `add_worker_to_shift` and
`remove_worker_from_shift` amend it, `log_shift_event` records what the foreman
did about the conditions, and `end_shift` closes it with a signature and writes
the payroll rows. `list_shifts` and `get_shift` read it back.

────────────────────────────────────────────────────────────────────────────
WHY THE FOREMAN IS THE ONLY ACTOR
────────────────────────────────────────────────────────────────────────────

There is no `clock_in` here and there will not be one. Three reasons, argued at
length in `erpnext_mcp/shifts.py`, of which the first decides it: OAR
437-004-1131 puts the water, shade, rest-cycle and observation obligations on a
NAMED responsible person, and FSMA §112.161(b) asks that person to sign. A crew
of thirty each clocking themselves in is a shift with thirty people responsible
for the record, which is a shift with nobody responsible for it — and the
observable failure is that nobody logged the water break because everybody
assumed somebody else had.

Per-worker attendance is not lost to this. Every crew row carries its own
`joined_at` and `left_at`, `remove_worker_from_shift` sets the second rather than
deleting the row, and the close writes one Attendance record per person for
their own span.

────────────────────────────────────────────────────────────────────────────
`end_shift` REQUIRES A SIGNATURE AND THAT IS THE POINT OF `end_shift`
────────────────────────────────────────────────────────────────────────────

An unsigned close is just an UPDATE statement setting a timestamp. The signature
is what makes the close an attestation — the supervisor saying, at the moment
they say it, that this is what happened — and §112.161(b) asks for a review that
is dated and signed rather than merely recorded. So the tool refuses without one
and says so before it writes anything.

WHAT IT DOES NOT REFUSE: a shift with obligations unmet. A day where the shade
trailer broke down and the crew was sent home at eleven is a real shift with a
real gap, and a tool that would not let the gap be recorded would produce either
a false record or no record. Same posture the training tools take towards a
missing §112.161 element — state it, keep it.

────────────────────────────────────────────────────────────────────────────
THE GUARDS ARE `create_employee`'s, SHARED RATHER THAN RESTATED
────────────────────────────────────────────────────────────────────────────

`employee.require_shift_role` and `employee.require_company_scope`, imported. A
shift is a personnel record before it is a compliance record: it names who was at
work and for how long, it is read in a wage claim, and forming one for an entity
you cannot see would put a crew on a register you cannot read. Reads are scoped
the same way, so a scoped account listing "every shift" gets its own entity's.

THE ROLE LIST IS `employee.SHIFT_ROLES` AND NOT `employee.HR_ROLES`, which is
the one place these tools differ from the personnel ones they borrow the guards
from. It is that list plus Foreman and Crew Leader, argued where it is defined:
-1131 puts the water, shade, rest-cycle and observation obligations on the NAMED
supervisor, this app's own role table already grants Foreman full permission on
`Farm Shift` for that reason, and a supervisor who cannot open the shift cannot
discharge the obligation the rule names them for — nor close it, which is what
writes the crew's Attendance rows for the day. Hiring stays where it was: none
of these two roles can create or edit an Employee, an I-9 or a W-4.
"""

from __future__ import annotations

import itertools

import frappe

from .. import breaks as breaks_mod
from .. import compat, geo, minors, shifts, timezones
from ..args import as_bool, as_choice, as_date, as_float, as_int, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from ..services import push as push_service
from . import employee as employee_tool
from . import shadow_log

DOCTYPE = shifts.DOCTYPE

#: Most shifts any one read returns. A shift register is read to answer a
#: question about a period or a crew, not to be exported.
RECORD_CAP = 500

#: Most breadcrumbs one `get_shift_track` call returns. Deliberately much larger
#: than `RECORD_CAP`: a track is not a register, and the useful cadence for a
#: crew's movements is a fix every thirty seconds to two minutes — so a nine-hour
#: shift is between two hundred and eleven hundred points, and a limit that cut
#: at five hundred would silently drop the afternoon of a normal day.
TRACK_CAP = 5000

#: How long a silence has to be before a track REPORTS it as a gap. Ten minutes,
#: because the cadences a phone actually posts at are all well under it — so a
#: quiet stretch past ten minutes is a device in a pocket, out of signal, or off,
#: and a straight line drawn across it is a line the crew did not walk.
TRACK_GAP_SECONDS = 10 * 60

#: How poor a reported accuracy has to be before it is worth saying so. Fifty
#: metres is about half an orchard block: past it a fix cannot settle which side
#: of a boundary somebody was on, which is most of what a track gets asked. It is
#: a NOTE and never a gate — see the doctype controller on why a bad fix is kept.
POOR_ACCURACY_METRES = 50.0

#: Most workers one `start_shift` call will roster. Sixty is a large crew on a
#: single block; past it the caller has almost certainly passed a company roster
#: rather than the people who turned up, and forming a shift for everybody on the
#: payroll would write sixty wrong Attendance rows when it closed.
CREW_CAP = 60


def _require() -> None:
	compat.require_doctype(
		DOCTYPE,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _readable_companies(actor: str) -> list:
	"""Companies this principal may see, or [] meaning unrestricted.

	Frappe's own rule: no User Permission means unrestricted, which is what every
	Desk surface already does. `api/guard.py` inverts that for the mobile methods
	and says why; this is not that surface.
	"""
	from .. import roles

	return roles.companies_for(actor) or []


def _refuse_a_second_open_shift(employee: str, employee_name: str = "", exclude: str = "") -> None:
	"""Nobody is on two open shifts at once, and the reason is a wage record.

	THE SAME-CREW DEDUP IS NOT THIS CHECK AND DOES NOT COVER IT. The Farm Shift
	controller refuses one Employee twice on ONE crew — two rows on one form,
	visible to whoever is looking at that form. This is the other shape, and it
	is the one that actually happens on a farm with more than one crew running:
	the block foreman rosters Ana at six, the packing shed's lead rosters her at
	ten on a shift of their own, and NEITHER FORM SHOWS THE OTHER. Nothing is
	wrong with either record on its own.

	It goes wrong at the close. `end_shift` writes one submitted Attendance per
	crew row spanning that person's own joined_at to their own left_at, so two
	open shifts become two overlapping Attendance days for one person for one
	date — which payroll pays, because each row is a correct row about a shift
	that really happened. Nobody finds it in the shift register, because the
	register is two entries that both look right.

	REFUSED RATHER THAN WARNED, which is the opposite of what this file does with
	an unverified I-9, and the difference is what the mistake costs. A crew
	member without a Verified I-9 is a paperwork gap that a hard refusal would
	turn into a crew stranded mid-harvest; a double roster is money out of the
	door and a wage record that cannot be defended. The refusal names the other
	shift, because the fix is always to close it or clock them out of it first.
	"""
	others = shifts.open_shifts_for(employee, exclude=exclude)
	if not others:
		return
	other = others[0]
	where = f" at {other.get('location')}" if other.get("location") else ""
	raise ToolError(
		f"{employee_name or employee} is already on {other['name']}, an OPEN shift"
		f"{where} that started at {other.get('start_datetime')} under "
		f"{other.get('foreman_name') or other.get('foreman')}, and they have not been clocked out "
		"of it. Nobody works two shifts at once, and the reason this is refused rather than "
		"recorded is what happens when both close: end_shift writes one Attendance row per crew "
		"row, so the same person gets two overlapping days for one date and payroll pays both. "
		f"Clock them out with remove_worker_from_shift on {other['name']}, or close that shift "
		"first. Nothing was changed."
	)


#: Every spelling a caller has ever used for a shift docname, tried in order.
#: v0.98.0, AND IT IS A LIVE BUG REPORT RATHER THAN TIDYING. `get_shift` resolved
#: on `("name", "name", "farm_shift")` — `shift` was never consulted at all, even
#: though the registry advertises it as an alias and `api/mobile.get_shift`
#: passes exactly that key. So the shift read on the mobile surface answered
#: "farm_shift is required" for every call a handset made, which is the identical
#: failure v0.96.0 fixed on `end_shift` (item 1) at a different door. Naming the
#: spellings once means the next tool cannot resolve on a different subset of
#: them.
SHIFT_KEYS = ("shift", "name", "farm_shift")


def _resolve_shift(args: dict, key: str = "shift") -> dict:
	name = ""
	for candidate in (key, *SHIFT_KEYS):
		name = as_str(args, candidate)
		if name:
			break
	if not name:
		# The refusal keeps naming `farm_shift`, because that is the spelling the
		# handset sends and the one somebody debugging a 400 will search for.
		as_str(args, "farm_shift", required=True)
	name = name.strip()
	if not frappe.db.exists(DOCTYPE, name):
		raise ToolError(
			f"no {DOCTYPE} called {name!r} on this site. list_shifts has the register; a docname "
			"looks like SHIFT-2026-0001. Nothing was changed."
		)
	return _shift_row(name)


def _shift_row(name: str) -> dict:
	"""One Farm Shift as a dict, by docname, with no resolution and no checking."""
	return dict(
		frappe.db.get_value(DOCTYPE, name, compat.existing_fields(DOCTYPE, shifts.FIELDS), as_dict=True) or {}
	)


def _resolve_shift_for_update(args: dict, key: str = "shift") -> dict:
	"""Resolve the shift, take its row lock, and read it again UNDER that lock.

	THE RE-READ IS THE POINT AND NOT TIDINESS. `shifts.lock_shift` makes a read
	authoritative; it cannot refresh one already taken. The first resolution
	above it exists only to turn whatever spelling arrived into a docname, and
	its `end_datetime` — the value `is_open` and every close guard turn on — was
	read before anybody was blocked, so it is exactly as stale as the value this
	whole mechanism exists to stop being trusted.

	EVERY TOOL THAT LOADS THE SHIFT DOCUMENT AND SAVES IT CALLS THIS, not just
	the two that touch `crew`. Frappe rewrites a child table by deleting its rows
	and re-inserting them, so a save from ANY of them — a break logged, an event
	timestamped, a close signed — rewrites the crew as it was when that caller
	loaded the document. A lock only serialises the writers that take it, and one
	tool left outside it is enough to drop a worker off a roster. See
	`shifts.lock_shift` for the failure it produces.
	"""
	row = _resolve_shift(args, key)
	shifts.lock_shift(str(row.get("name") or ""))
	return _shift_row(str(row.get("name") or "")) or row


def file_reference(value: str, label: str) -> str:
	"""A File docname or file_url, checked to point at something real.

	A DOCNAME IS CHECKED AND A URL IS TAKEN AS GIVEN, which is exactly what
	`inspections.normalise_evidence` does and for the same two reasons. A docname
	this site has never heard of is a typo or a token from another install, and a
	signature pointing at nothing satisfies the contract and proves nothing —
	which is the one kind of missing evidence nobody discovers until an auditor
	clicks it. A URL, though, is a path: a file served from somewhere this site
	does not have a File row for is a real possibility on a bench that has been
	moved, and refusing it would leave a shift OPEN because its signature could
	not be resolved. An open shift is a worse outcome than an unresolvable path,
	because the timeline keeps growing against it.

	Both spellings arrive in practice: the iOS app gets a File docname back from
	`finalize_staged_file`, and a Desk user has a URL.
	"""
	value = str(value or "").strip()
	if not value:
		return ""
	try:
		if frappe.db.exists("File", value):
			return str(frappe.db.get_value("File", value, "file_url") or value)
	except Exception:  # pragma: no cover - a site that cannot read its own File table
		return value
	if value.startswith("/") or value.startswith("http"):
		return value
	raise ToolError(
		f"{label} points at {value!r}, which is not a File on this site and is not a path. "
		"Upload it with stage_file_chunk and commit_staged_file first — a signature pointing at "
		"nothing satisfies the contract and proves nothing, and nobody finds that out until an "
		"auditor clicks it. Nothing was changed."
	)


def _when(args: dict, key: str) -> str:
	"""A timestamp argument, defaulting to now.

	`now()` rather than `today()`: everything on a shift is answered at the
	minute — an hour between water breaks and three hours between them are
	different shifts, and only the timestamps say which this was.
	"""
	return as_str(args, key) or frappe.utils.now()


#: The two states this app carries labour rules for — break schedules,
#: withholding tables and the minor hour ceilings. MOVED UP FROM THE BREAK POLICY
#: BLOCK in v0.106.0, because `_work_state_argument` now reads it four hundred
#: lines earlier than `create_break_policy` does and a constant defined below its
#: first caller reads as an accident. Same tuple, same two values.
_VALID_STATES = ("OR", "WA")


def _work_state_argument(args: dict, key: str = "work_state") -> str:
	"""`OR` or `WA`, upper-cased, or "" where none was given.

	REFUSES A THIRD STATE rather than storing it. The column is a Select with
	exactly these two options, so a value outside them is dropped by Frappe on
	save and the caller is never told — which would leave a shift that reports no
	state at all after a call that named one, and the minor checks silently back
	on the strictest table. The same pair `create_break_policy` validates against.
	"""
	wanted = as_str(args, key).strip().upper()
	if not wanted:
		return ""
	if wanted not in _VALID_STATES:
		raise ToolError(
			f"{key} must be one of {', '.join(_VALID_STATES)}, got {wanted!r}. It decides which "
			"state's break schedule and which minor hour ceilings this shift is read against — "
			"Oregon allows a 16- or 17-year-old sixty hours a week and Washington fifty — so a "
			"value this app does not know would be dropped on save and the shift would report "
			"none. Nothing was created."
		)
	return wanted


def _crew_argument(raw, label: str = "crew_employees") -> list:
	"""Whatever the caller sent as a crew, as a list of Employee docnames."""
	if raw in (None, "", []):
		return []
	if isinstance(raw, str):
		raw = [part.strip() for part in raw.split(",") if part.strip()]
	if not isinstance(raw, (list, tuple)):
		raise ToolError(f"{label} must be a list of employees. Got {type(raw).__name__}.")
	if len(raw) > CREW_CAP:
		raise ToolError(
			f"{label} names {len(raw)} people, past the {CREW_CAP} cap. Sixty is a large crew on "
			"one block; past it this is almost certainly a company roster rather than the people "
			"who turned up, and every extra name becomes a wrong Attendance row when the shift "
			"closes. Nothing was created."
		)
	out = []
	for index, entry in enumerate(raw):
		if isinstance(entry, dict):
			person = str(entry.get("employee") or entry.get("name") or "").strip()
			role = str(entry.get("role") or "Worker").strip() or "Worker"
			joined = str(entry.get("joined_at") or "").strip()
		else:
			person, role, joined = str(entry or "").strip(), "Worker", ""
		if not person:
			raise ToolError(f"{label}[{index}] names nobody.")
		out.append(
			{
				"employee": employee_tool.resolve_employee(person),
				"role": as_choice(shifts.CREW_DOCTYPE, "role", role, "role"),
				"joined_at": joined,
			}
		)
	return out


#: `i9_status` values that mean the person may lawfully work. Everything else —
#: `Pending`, `Expired`, `N-A`, or the column simply absent on a site that has
#: not run `install_compliance_fields` — is not evidence of readiness.
_I9_CLEARED = ("Verified",)


def minor_findings(
	employee: str, employee_name: str, shift_row: dict, joined: str, exclude: str = ""
) -> dict:
	"""What being under eighteen says about putting this person on this shift.

	v0.98.0. RETURNS FINDINGS; IT DOES NOT REFUSE. Two callers want two different
	answers out of one piece of arithmetic — `add_worker_to_shift` refuses on a
	`blocked` finding, because it is being asked about ONE named person and the
	shift goes on existing without them, while `start_shift` reports the same
	finding, because refusing there would mean no shift record at all for a crew
	that is standing in the block whatever this app says. The regulation is the
	same in both; what differs is what a refusal would destroy.

	THREE CHECKS, AND EACH ONE NAMES ITS OWN CITATION (see `minors.py`):

	  * the clock — a 14- or 15-year-old may not work before 07:00 or after
	    19:00 (29 CFR 570.35), checked against the moment they join and, where
	    the shift already has one, the moment it ends;
	  * the day — 8 hours for under-16, 10 for 16-17, counted across every shift
	    they are already on today (ORS 653.315 / OAR 839-021-0104);
	  * the week — 40 and 60 respectively, over the Monday-start workweek.

	AN UNKNOWN DATE OF BIRTH IS A FINDING AND NOT A BLOCK. `is_minor` is
	three-valued and None means nobody recorded it; refusing on that would stop a
	farm rostering its adult crew because a column is empty, and clearing on it
	silently would be the failure this whole item exists to close. So it comes
	back as `date_of_birth_missing`, which the callers put in front of the
	foreman.
	"""
	when = str(joined or shift_row.get("start_datetime") or "")
	born = None
	if compat.doctype_exists("Employee") and compat.has_field("Employee", "date_of_birth"):
		row = (
			frappe.db.get_value("Employee", employee, ["employee_name", "date_of_birth"], as_dict=True) or {}
		)
		born = row.get("date_of_birth")
		# THE NAME IS LOOKED UP WHERE THE CALLER DID NOT HAVE ONE, because every
		# sentence this function produces is read by a foreman standing next to
		# the person — and "HR-EMP-00091 is 15" is a refusal nobody can act on
		# without going to look somebody up. `start_shift` calls this with no
		# name in hand; `add_worker_to_shift` already has one.
		employee_name = employee_name or str(row.get("employee_name") or "")
	# WHICH STATE'S RULES. Oregon lets a 16- or 17-year-old work sixty hours a
	# week and Washington fifty, and Washington puts a 05:00-22:00 clock on that
	# band where Oregon has none — so a check that did not read this column was
	# answering for one state on every farm. Where the shift carries no
	# `work_state` the strictest of the two is used and `minors.state_note` says
	# so in the refusal, because an unactionable refusal is one a foreman routes
	# around. See `minors.LIMITS_BY_STATE`.
	work_state = str(shift_row.get("work_state") or "")
	described = minors.describe(born, when[:10] or frappe.utils.today(), work_state)
	out = {"employee": employee, "employee_name": employee_name or employee, **described, "blocked": []}
	if described["is_minor"] is None:
		out["date_of_birth_missing"] = (
			f"{employee_name or employee} has no date of birth on file, so this app cannot say "
			"whether the minor hour and time-of-day limits apply to them. That is a gap in the "
			"record rather than a finding about the person — set it with update_employee."
		)
		return out
	if not described["is_minor"]:
		return out

	band = described["minor_band"]
	clock = minors.time_of_day_violation(band, when, shift_row.get("end_datetime") or "", work_state)
	if clock:
		out["blocked"].append(f"{employee_name or employee} is {described['age']} — {clock}")

	worked = shifts.hours_worked_by(employee, when[:10], exclude=exclude or str(shift_row.get("name") or ""))
	out["hours_today"] = worked["today"]
	out["hours_this_week"] = worked["week"]
	out["other_shifts_today"] = [entry for entry in worked["shifts"] if entry["day"] == worked["day"]]

	# The span this shift ADDS, where the shift already has an end time. Where it
	# does not — the ordinary case, a shift that is still running — nothing is
	# projected: an invented finishing time would produce a refusal nobody can
	# check, and the hours already worked are a fact.
	projected = shifts.hours_between(when, str(shift_row.get("end_datetime") or "")) or 0.0
	out["hours_added_by_this_shift"] = projected

	over = minors.hours_violation(band, worked["today"] + projected, worked["week"] + projected, work_state)
	if over:
		out["blocked"].append(f"{employee_name or employee} is {described['age']} — {over}")
	else:
		warning = minors.hours_warning(
			band, worked["today"] + projected, worked["week"] + projected, work_state
		)
		if warning:
			out["approaching_limit"] = warning

	# THE SEVENTH DAY, counted from the distinct days the workweek's shifts fall
	# on rather than from a column — `hours_worked_by` already walked them. A
	# WARNING and never a block: Washington excepts dairy, livestock, hay harvest
	# and irrigation-dependent crop work from its six-day rule and this app cannot
	# tell which a shift is. Oregon carries no figure, so this is silent there.
	days = {entry["day"] for entry in worked["shifts"] if entry.get("day")}
	days.add(when[:10])
	out["days_this_week"] = len(days)
	crowded = minors.days_warning(band, len(days), work_state)
	if crowded:
		out["days_warning"] = f"{employee_name or employee} is {described['age']} — {crowded}"

	# THE MISSING COLUMN, reported beside the finding it changed. Only where
	# there IS a finding — a note about `work_state` on every clean roster row
	# would be the kind of advice that trains people to skip the whole block.
	if out["blocked"] or out.get("approaching_limit"):
		note = minors.state_note(work_state)
		if note:
			out["work_state_note"] = note
	return out


def _refuse_a_minor_over_the_limit(findings: dict) -> None:
	"""Turn `minor_findings` into the refusal `add_worker_to_shift` owes a foreman."""
	if not findings.get("blocked"):
		return
	limits = findings.get("minor_limits") or {}
	raise ToolError(
		"; ".join(findings["blocked"])
		+ ". Nothing was changed. This is a limit on what may lawfully be SCHEDULED, so it is a "
		"refusal rather than a note: the ceiling is "
		f"{limits.get('daily_hours')} hour(s) a day and {limits.get('weekly_hours')} a week for the "
		f"{findings.get('minor_band')} band"
		+ (f", between {limits['earliest']} and {limits['latest']}" if limits.get("earliest") else "")
		+ f" ({limits.get('citation')}). If they have already worked today on another crew, close "
		"or clock them out of that shift first; if the date of birth on their record is wrong, "
		"correct it with update_employee."
		+ (f" {findings['work_state_note']}" if findings.get("work_state_note") else "")
	)


def _i9_unverified(employees: list[str]) -> list[dict]:
	"""Who on this list has no Verified I-9 on file, or [] where nobody knows.

	NOT A GATE. `start_shift` and `add_worker_to_shift` still put the crew on
	the clock — a hard refusal here would strand a harvest crew mid-morning
	over a paperwork column most sites do not even read. It is a WARNING a
	foreman sees at the moment they can still do something about it, which is
	the gap the 2026-08-07 cross-system review named: nothing downstream of
	onboarding ever consulted `i9_status`, so a person with no I-9 at all could
	be rostered, badged and paid without anyone being told.

	Silent (returns []) where the column is not installed, exactly like every
	other `compat.has_field` guard in this app — an absent column is not
	evidence that everyone is unverified, it is a site that has not opted in.
	"""
	if not employees or not compat.has_field("Employee", "i9_status"):
		return []
	rows = frappe.db.get_all(
		"Employee",
		filters={"name": ("in", employees)},
		fields=["name", "employee_name", "i9_status"],
	)
	return [
		{
			"employee": row["name"],
			"employee_name": row.get("employee_name") or row["name"],
			"i9_status": row.get("i9_status") or "",
		}
		for row in rows
		if (row.get("i9_status") or "") not in _I9_CLEARED
	]


def _crew_note(described: dict) -> str:
	size = described.get("crew_size") or 0
	if not size:
		return (
			"NOBODY IS ON THIS CREW YET. A foreman opening a shift before the crew arrives is the "
			"ordinary case rather than an error — add_worker_to_shift rosters them as they turn "
			"up. But a shift closed with an empty crew writes no Attendance at all, and it is "
			"evidence about nobody."
		)
	return f"{size} on the crew, {described.get('still_on_shift')} of them still on the shift."


# ── 1. start_shift ──────────────────────────────────────────────────────────
def start_shift(args: dict) -> ToolResult:
	"""Form a crew at a place, and start the exposure period compliance is read against."""
	_require()
	compat.require_doctype("Employee", "It comes with the Frappe HR (hrms) app.")
	actor = employee_tool.require_shift_role()

	foreman = employee_tool.resolve_employee(as_str(args, "foreman", required=True))
	row = frappe.db.get_value("Employee", foreman, ["employee_name", "company", "status"], as_dict=True) or {}
	company = resolve_company(as_str(args, "company") or str(row.get("company") or ""), required=True)
	employee_tool.require_company_scope(actor, company)
	if row.get("company") and str(row["company"]) != company:
		raise ToolError(
			f"{foreman} ({row.get('employee_name')}) is employed by {row['company']}, and this "
			f"call names {company}. A shift belongs to the entity whose crew worked it — filing "
			"it against another one puts the evidence in a packet that will be handed to an "
			"inspector asking about a different company. Nothing was created."
		)

	crew = _crew_argument(args.get("crew_employees") or args.get("crew"))
	start = _when(args, "start_datetime")
	work_state = _work_state_argument(args)

	# THE CREW IS CHECKED AGAINST THE COMPANY BEFORE ANYTHING IS WRITTEN, because a
	# shift half-created and then refused on its ninth crew member leaves an open
	# shift nobody meant to open — and an open shift is what the v0.19.4 weather
	# sweep walks and what `list_shifts` reports as work in progress.
	for entry in crew:
		theirs = str(frappe.db.get_value("Employee", entry["employee"], "company") or "")
		if theirs and theirs != company:
			raise ToolError(
				f"{entry['employee']} is employed by {theirs} and this shift belongs to {company}. "
				"A crew list crossing entities produces Attendance rows on a payroll register "
				"that did not employ the person. Nothing was created."
			)
		# AND NOBODY IS ROSTERED ONTO A SECOND OPEN SHIFT. Checked in the same
		# pre-write pass and for the same reason it is here rather than after the
		# insert: a shift refused on its ninth crew member must leave no shift
		# behind. See `_refuse_a_second_open_shift` — the same-crew dedup the
		# controller does is a different check and does not reach this.
		_refuse_a_second_open_shift(
			entry["employee"],
			str(frappe.db.get_value("Employee", entry["employee"], "employee_name") or ""),
		)

	doc = frappe.new_doc(DOCTYPE)
	doc.foreman = foreman
	doc.foreman_name = row.get("employee_name") or foreman
	doc.company = company
	doc.location = as_str(args, "location")
	doc.farm_location_gps = as_str(args, "farm_location_gps")
	doc.shift_type = as_choice(DOCTYPE, "shift_type", as_str(args, "shift_type") or "General", "shift_type")
	doc.start_datetime = start
	# v0.106.0. THE COLUMN NOTHING EVER WROTE. `work_state` has been on this
	# doctype since v0.58.0 and is read by three things — `get_break_schedule`'s
	# policy fallback, `list_employees_by_work_state`, and now the minor hour
	# checks — and `start_shift` never set it, so it was empty on every shift this
	# app has ever created and all three were answering from a blank. It is
	# OPTIONAL rather than required: a foreman opening a shift at five in the
	# morning should not be stopped by a field, and the readers each say what
	# they did without one.
	if work_state:
		doc.work_state = work_state
	for entry in crew:
		doc.append(
			"crew",
			{
				"employee": entry["employee"],
				"role": entry["role"],
				# DEFAULTS TO THE SHIFT'S OWN START rather than to now. Everybody the
				# foreman rostered at the beginning was there at the beginning, and
				# stamping them with the moment the API call landed would shave
				# minutes off every one of their days.
				"joined_at": entry["joined_at"] or start,
			},
		)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	described = shifts.describe(dict(doc.as_dict()), with_children=True)
	data = {
		**described,
		"actor": actor,
		"crew_note": _crew_note(described),
		"note": (
			f"Shift {doc.name} is OPEN. Everything logged against it from now until end_shift is "
			"evidence about this exposure period — log_shift_event records the water breaks, "
			"shade breaks, rest cycles and observations OAR 437-004-1131 asks about, and "
			"end_shift closes it with the supervisor's signature."
		),
		"next_step": (
			"log_shift_event as things happen. Logging at the time is the whole value: a timeline "
			"written from memory in the evening is what an investigator discounts."
		),
	}
	if not doc.farm_location_gps:
		data["weather_note"] = (
			"No farm_location_gps on this shift, so it will get NO weather timeline when v0.19.4 "
			"wires the fetch — the service asks Open-Meteo what the conditions are at a place, and "
			"there is no place here. A point-in-time temperature is a data point and a timeline is "
			"a defence; set the coordinates while the shift is still open."
		)
	# v0.98.0. REPORTED AND NOT REFUSED, which is the opposite of what
	# `add_worker_to_shift` does with the identical arithmetic — see
	# `minor_findings`. A crew is standing in the block; a server that would not
	# open a shift for them because one fifteen-year-old is over their weekly
	# hours produces NO record of the afternoon at all, which is worse evidence
	# than a record carrying the finding. The finding is loud and it names the
	# person, the citation and the figure.
	minors_found = [
		minor_findings(
			entry["employee"], "", dict(doc.as_dict()), entry["joined_at"] or start, exclude=doc.name
		)
		for entry in crew
	]
	over = [entry for entry in minors_found if entry.get("blocked")]
	unknown = [entry for entry in minors_found if entry.get("date_of_birth_missing")]
	on_crew = [entry for entry in minors_found if entry.get("is_minor")]
	# NOT `minors_on_crew`, WHICH `shifts.describe` ALREADY SETS TO A COUNT. Two
	# keys of one name on one payload is a shape that changes type depending on
	# which line ran last, and it is exactly the class of failure wave 1 was
	# seven instances of. The count is the number; this is the detail.
	if on_crew:
		data["minor_crew_findings"] = on_crew
	if over:
		data["minor_limits_exceeded"] = [
			{"employee": entry["employee"], "reasons": entry["blocked"]} for entry in over
		]
		data["minor_note"] = (
			f"{len(over)} worker(s) under eighteen on this crew are over a scheduling limit: "
			+ "; ".join(reason for entry in over for reason in entry["blocked"])
			+ ". The shift was still opened — a crew in the block with no record of the "
			"afternoon is worse evidence than a record carrying this finding — but the hours "
			"are a limit on what may lawfully be scheduled, not a note. Clock them out of the "
			"other shift, or send them home."
		)
	if unknown:
		data["date_of_birth_missing"] = [entry["employee"] for entry in unknown]

	unverified = _i9_unverified([entry["employee"] for entry in crew])
	summary_suffix = ""
	if unverified:
		data["i9_unverified"] = unverified
		names = ", ".join(f"{row['employee_name']} ({row['i9_status'] or 'no I-9'})" for row in unverified)
		data["i9_note"] = (
			f"{len(unverified)} of this crew has no Verified I-9 on file: {names}. This is a "
			"WARNING, not a block — the shift was still opened, because a hard refusal here would "
			"strand a crew mid-harvest over a paperwork column. Verify or reverify before this "
			"person's next shift."
		)
		summary_suffix = f" — {len(unverified)} without a Verified I-9"
	return ToolResult(
		data=data,
		summary=(
			f"started {doc.shift_type} shift {doc.name} at {doc.location or 'an unnamed location'} "
			f"under {described['foreman_name']} with {described['crew_size']} on the crew{summary_suffix}"
		),
		docstatus_delta="none → 0 (created)",
	)


# ── 2. add_worker_to_shift ──────────────────────────────────────────────────
def add_worker_to_shift(args: dict) -> ToolResult:
	"""Roster somebody onto a shift already running — a late arrival, or a transfer."""
	_require()
	actor = employee_tool.require_shift_role()
	row = _resolve_shift_for_update(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))
	if not shifts.is_open(row):
		raise ToolError(
			f"{row['name']} ended at {row.get('end_datetime')}. Nobody joins a shift that is over "
			"— and the Attendance rows for this shift have already been written, so a crew row "
			"added now would be a person with no payroll day. Start a new shift. Nothing was "
			"changed."
		)

	person = employee_tool.resolve_employee(as_str(args, "employee", required=True))
	theirs = frappe.db.get_value("Employee", person, ["employee_name", "company"], as_dict=True) or {}
	if theirs.get("company") and str(theirs["company"]) != str(row.get("company") or ""):
		raise ToolError(
			f"{person} ({theirs.get('employee_name')}) is employed by {theirs['company']} and this "
			f"shift belongs to {row.get('company')}. Nothing was changed."
		)

	doc = frappe.get_doc(DOCTYPE, row["name"])
	for entry in doc.crew or []:
		if str(entry.get("employee")) == person:
			raise ToolError(
				f"{theirs.get('employee_name') or person} is already on this crew, joined at "
				f"{entry.get('joined_at')}"
				+ (f" and left at {entry.get('left_at')}" if entry.get("left_at") else "")
				+ ". Two rows for one person become two Attendance days when the shift closes. "
				"Somebody who left and came back is one row spanning both — clear their left_at "
				"instead. Nothing was changed."
			)

	# THE SAME QUESTION ASKED OF EVERY OTHER SHIFT ON THE SITE, which the loop
	# above cannot answer: it reads this crew, and a worker rostered onto a
	# second crew is invisible from here. `remove_worker_from_shift` on the other
	# shift is the fix, and the refusal names it.
	_refuse_a_second_open_shift(person, theirs.get("employee_name") or person, exclude=row["name"])

	# DEFAULTS TO NOW, WHICH IS THE OPPOSITE OF `start_shift`'s DEFAULT AND IS
	# RIGHT FOR THE SAME REASON. A worker rostered at the beginning was there at
	# the beginning; a worker added mid-shift arrived when somebody said so.
	joined = _when(args, "joined_at")

	# v0.98.0. AND THIS IS WHERE A MINOR'S DAY IS CHECKED, before the append and
	# after everything cheaper. It REFUSES here and merely reports in
	# `start_shift`, which is not an inconsistency: this call is about one named
	# person and the shift carries on without them, so a refusal costs a name off
	# a crew; there it would cost the whole shift record for a crew already in the
	# block. What a refusal buys is the thing a note cannot — ORS 653.315 and 29
	# CFR 570.35 are limits on what may be SCHEDULED, and a foreman who is told
	# after the fact that the afternoon was unlawful has been told too late.
	minor = minor_findings(person, str(theirs.get("employee_name") or ""), row, joined)
	_refuse_a_minor_over_the_limit(minor)

	doc.append(
		"crew",
		{
			"employee": person,
			"employee_name": theirs.get("employee_name") or person,
			"role": as_choice(shifts.CREW_DOCTYPE, "role", as_str(args, "role") or "Worker", "role"),
			"joined_at": joined,
			"notes": as_str(args, "notes"),
		},
	)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	described = shifts.describe(dict(doc.as_dict()), with_children=True)
	late = shifts.hours_between(str(row.get("start_datetime") or ""), joined)
	data = {
		**described,
		"actor": actor,
		"added": {
			"employee": person,
			"employee_name": theirs.get("employee_name") or person,
			"joined_at": joined,
			"hours_after_shift_start": late,
		},
		"note": (
			f"{theirs.get('employee_name') or person} is on the crew from {joined}. Their "
			"Attendance record will span from there rather than from the shift's start — the "
			"shift is the crew envelope and the row is the person's own day."
		),
	}
	if late is not None and late >= 1:
		data["acclimatization_note"] = (
			f"They joined {late} hour(s) into the shift. If the shift has already crossed the "
			f"{shifts.HEAT_THRESHOLD_F:.0f} °F heat index, somebody arriving mid-shift has had "
			"none of the morning's water breaks and none of the acclimatization the crew has "
			"— OAR 437-004-1131(g) is about exactly this person."
		)
	# The findings that did NOT block. `date_of_birth_missing` is the common one
	# and it is the one worth surfacing: a crew whose ages nobody recorded is a
	# crew this app cannot check, and saying so on the roster call is the only
	# moment somebody is looking at the right screen to fix it.
	if minor.get("is_minor"):
		data["minor"] = minor
	if minor.get("approaching_limit"):
		data["minor_note"] = (
			f"{minor['employee_name']} is {minor['age']} — {minor['approaching_limit']} "
			f"({(minor.get('minor_limits') or {}).get('citation')})."
		)
	if minor.get("date_of_birth_missing"):
		data["date_of_birth_missing"] = minor["date_of_birth_missing"]

	summary_suffix = ""
	unverified = _i9_unverified([person])
	if unverified:
		data["i9_unverified"] = unverified
		data["i9_note"] = (
			f"{theirs.get('employee_name') or person} has no Verified I-9 on file "
			f"(status: {unverified[0]['i9_status'] or 'no I-9'}). This is a WARNING, not a block — "
			"they were still rostered. Verify or reverify before their next shift."
		)
		summary_suffix = " — no Verified I-9"
	return ToolResult(
		data=data,
		summary=(
			f"added {theirs.get('employee_name') or person} to {row['name']} at {joined} "
			f"({described['crew_size']} on the crew){summary_suffix}"
		),
		docstatus_delta="0 → 0 (amended)",
	)


# ── 3. remove_worker_from_shift ─────────────────────────────────────────────
def remove_worker_from_shift(args: dict) -> ToolResult:
	"""End one worker's time on a shift that continues without them.

	IT SETS `left_at`; IT DOES NOT DELETE THE ROW. Deleting would destroy the only
	record that this person was on the shift at all — which is the record a wage
	claim turns on, and the record that says who was exposed on a hot afternoon
	before they were sent home. The name of the tool is the operational verb and
	the storage is the compliance one, and the two are allowed to differ.
	"""
	_require()
	actor = employee_tool.require_shift_role()
	row = _resolve_shift_for_update(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	person = employee_tool.resolve_employee(as_str(args, "employee", required=True))
	doc = frappe.get_doc(DOCTYPE, row["name"])
	target = None
	for entry in doc.crew or []:
		if str(entry.get("employee")) == person:
			target = entry
			break
	if target is None:
		raise ToolError(
			f"{person} is not on the crew of {row['name']}. get_shift lists who is. Nothing was changed."
		)
	if target.get("left_at") and not str(args.get("left_at") or "").strip():
		raise ToolError(
			f"{target.get('employee_name') or person} already left {row['name']} at "
			f"{target['left_at']}. Pass left_at explicitly to correct that time; calling this "
			"twice with no time would silently move their departure to now and lengthen a day "
			"that has already ended. Nothing was changed."
		)

	left = _when(args, "left_at")
	target.left_at = left
	if as_str(args, "notes"):
		target.notes = as_str(args, "notes")
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	described = shifts.describe(dict(doc.as_dict()), with_children=True)
	hours = shifts.hours_between(str(target.get("joined_at") or ""), left)
	return ToolResult(
		data={
			**described,
			"actor": actor,
			"removed": {
				"employee": person,
				"employee_name": target.get("employee_name") or person,
				"joined_at": str(target.get("joined_at") or "") or None,
				"left_at": left,
				"hours_present": hours,
			},
			"note": (
				f"{target.get('employee_name') or person} is off the shift from {left}. THE ROW IS "
				"STILL THERE — it is the only record they were on this shift at all, which is what "
				"a wage claim turns on and what says who was exposed before they were sent home. "
				f"Their Attendance record will span {target.get('joined_at')} to {left}"
				+ (f", {hours} hour(s)." if hours is not None else ".")
			),
			"shift_note": (
				f"The shift continues with {described['still_on_shift']} still on it."
				if described["still_on_shift"]
				else "NOBODY IS LEFT ON THIS SHIFT. If the crew has gone home, end_shift closes it "
				"— an open shift with an empty crew keeps appearing as work in progress and keeps "
				"being fetched for weather it does not need."
			),
		},
		summary=(
			f"{target.get('employee_name') or person} left {row['name']} at {left}"
			+ (f" after {hours} hour(s)" if hours is not None else "")
		),
		docstatus_delta="0 → 0 (amended)",
	)


# ── 4. log_shift_event ──────────────────────────────────────────────────────
def log_shift_event(args: dict) -> ToolResult:
	"""Record one thing the foreman did about the conditions, at the moment it happened.

	THE TIMELINE IS THE EVIDENCE. Oregon's heat rule does not ask whether water
	was available in principle; it asks what happened during the shift, and four
	water breaks with timestamps answer that in a way an annual policy document
	never can. Logged at the time, because a timeline written from memory in the
	evening is what an investigator discounts.
	"""
	_require()
	actor = employee_tool.require_shift_role()
	row = _resolve_shift_for_update(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	event_type = as_choice(
		shifts.EVENT_DOCTYPE, "event_type", as_str(args, "event_type", required=True), "event_type"
	)
	when = _when(args, "event_datetime")
	logged_by = as_str(args, "logged_by")
	if logged_by:
		logged_by = employee_tool.resolve_employee(logged_by)
	else:
		# The foreman is the DEFAULT and not the assumption — a lead worker calling
		# a break at the far end of the block is the ordinary case, and `logged_by`
		# names them where somebody says so.
		logged_by = row.get("foreman")

	evidence = file_reference(
		as_str(args, "evidence_file_token") or as_str(args, "evidence_file"), "evidence_file_token"
	)

	producer_doctype = as_str(args, "producer_record_doctype")
	producer_name = as_str(args, "producer_record_name")
	if producer_name and not producer_doctype:
		raise ToolError(
			f"producer_record_name is {producer_name!r} and producer_record_doctype is empty. A "
			"docname with no doctype cannot be followed to anything — a packet builder reading "
			"this row would have a string and nowhere to look it up. Nothing was written."
		)

	doc = frappe.get_doc(DOCTYPE, row["name"])
	doc.append(
		"compliance_events",
		{
			"event_type": event_type,
			"event_datetime": when,
			"logged_by": logged_by or None,
			"description": as_str(args, "description"),
			"producer_record_doctype": producer_doctype or None,
			"producer_record_name": producer_name or None,
			"weather_snapshot_temp_f": args.get("weather_snapshot_temp_f"),
			"weather_snapshot_heat_index_f": args.get("weather_snapshot_heat_index_f"),
			"evidence_file": evidence or None,
		},
	)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	described = shifts.describe(dict(doc.as_dict()), with_children=True)
	same_type = [entry for entry in described["compliance_events"] if entry["event_type"] == event_type]
	data = {
		**described,
		"actor": actor,
		"logged": {
			"event_type": event_type,
			"event_datetime": when,
			"logged_by": logged_by or None,
			"producer_record": f"{producer_doctype} {producer_name}".strip() or None,
			"evidence_attached": bool(evidence),
		},
		"events_of_this_type": len(same_type),
		"note": (
			f"{len(described['compliance_events'])} event(s) on this shift's timeline, "
			f"{len(same_type)} of them {event_type}. The timeline is the evidence — 'water was "
			"available' is a policy and four timestamped breaks are a record."
		),
	}
	if not shifts.is_open(row):
		data["closed_shift_note"] = (
			f"{row['name']} was closed at {row.get('end_datetime')} and this event was added "
			"afterwards. Recorded rather than refused: an event remembered after the close is "
			"better on the record than off it. But it is dated by its own timestamp and the close "
			"is dated by its, and an inspector reading the two will see the order they happened in."
		)
	elif shifts.to_the_second(when) < shifts.to_the_second(row.get("start_datetime") or ""):
		data["timing_note"] = (
			f"This event is timestamped {when} and the shift started at {row.get('start_datetime')}"
			" — before the shift began. Kept as given, because a clock five minutes out is not a "
			"false record and refusing would mean the break goes unlogged rather than logged "
			"approximately. Worth correcting if it was not a clock."
		)
	return ToolResult(
		data=data,
		summary=f"logged {event_type} on {row['name']} at {when}",
		docstatus_delta="0 → 0 (amended)",
	)


# ── 4b. cancel_shift ────────────────────────────────────────────────────────
def cancel_shift(args: dict) -> ToolResult:
	"""Call a shift off: it was formed and then not worked.

	THE THIRD ENDING, AND IT IS NOT A CLOSE. `end_shift` says the crew worked and
	writes their Attendance; this says the crew did not, and writes none. Weather
	turned at 06:40 and everybody was sent home, the block was not ready, the
	sprayer never arrived — a shift that was opened and abandoned is an ordinary
	thing on a farm, and the two ways it was handled before this tool were both
	wrong. Leaving it open leaves a shift the weather sweep walks for ever and
	`list_shifts` reports as work in progress; closing it with a signature files
	an FSMA §112.161(b) attestation that a day happened, and writes an Attendance
	row per crew member for a day nobody worked.

	NO ATTENDANCE IS WRITTEN AND THAT IS THE POINT. If the crew DID work part of
	the day before being sent home, this is the wrong tool: close it with
	`end_shift` at the hour they stopped, which pays them for the hours they were
	there. The choice between the two tools is the choice between "they were paid
	for this" and "they were not", so it is made by a person and never inferred.

	THE CREW ROWS ARE KEPT. "They were rostered and stood down" is a fact
	somebody may need to answer a wage claim with — a crew list deleted on
	cancellation is the evidence that the people who turned up did turn up.

	THE REASON IS REQUIRED. A bare Cancelled flag is a gap somebody will be asked
	about, and the flag is reconstructable from the record where the sentence
	never is. The doctype asks for the same thing; this refuses first, so the
	message is about the decision rather than about a field.

	AN END TIME IS SET, because `status` is computed from `end_datetime` FIRST —
	a shift with the Cancelled box ticked and no end time is still Active, still
	walked by the weather sweep, still open. The default is now; pass
	`cancelled_at` where the crew was actually stood down.
	"""
	_require()
	actor = employee_tool.require_shift_role()
	row = _resolve_shift_for_update(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	if not shifts.is_open(row):
		already = shifts.status_for(row.get("end_datetime"), row.get("cancelled"))
		if already == shifts.STATUS_CANCELLED:
			raise ToolError(
				f"{row['name']} was already cancelled at {row.get('end_datetime')}: "
				f"{row.get('cancellation_reason') or 'no reason was recorded'}. Nothing was changed."
			)
		raise ToolError(
			f"{row['name']} was CLOSED at {row.get('end_datetime')} by a signed supervisor review, "
			"and closing wrote one Attendance record per crew member. Cancelling it now would "
			"claim the day was not worked while the payroll rows saying it was stay on the "
			"register — two answers to one question about somebody's wages. A day that was worked "
			"and then mis-recorded is corrected on the Attendance rows themselves. Nothing was "
			"changed."
		)

	reason = as_str(args, "cancellation_reason") or as_str(args, "reason")
	if not reason:
		raise ToolError(
			"cancellation_reason is required. 'Crew stood down at 06:40, heat index already 94 °F' "
			"is a record and a bare Cancelled flag is a gap somebody will be asked about — the flag "
			"can be reconstructed from the shift and the sentence cannot. THE SHIFT IS STILL OPEN "
			"and nothing was changed."
		)

	when = _when(args, "cancelled_at")
	if shifts.to_the_second(when) < shifts.to_the_second(row.get("start_datetime") or ""):
		raise ToolError(
			f"this call cancels the shift at {when} and it started at "
			f"{row.get('start_datetime')} — it would have been called off before it was formed. "
			"Nothing was changed."
		)

	doc = frappe.get_doc(DOCTYPE, row["name"])
	doc.cancelled = 1
	doc.cancellation_reason = reason
	doc.end_datetime = when
	if as_str(args, "foreman_notes"):
		doc.foreman_notes = as_str(args, "foreman_notes")
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	described = shifts.describe(dict(doc.as_dict()), with_children=True)
	crew = shifts.crew_of(row["name"])
	data = {
		**described,
		"actor": actor,
		"cancelled": {
			"cancelled_at": when,
			"cancellation_reason": reason,
			"crew_size": len(crew),
		},
		"attendance_created": 0,
		"note": (
			f"{row['name']} is CANCELLED and NO Attendance was written — this says the day was not "
			f"worked. {len(crew)} crew row(s) are kept, because 'they were rostered and stood down' "
			"is what answers a wage question about the people who turned up. If they DID work part "
			"of the day, this was the wrong tool: nothing here pays them for the hours they were "
			"there."
		),
	}
	if not crew:
		data["crew_note"] = (
			"No crew was rostered on this shift, so the cancellation is a shift that was opened "
			"and never filled. Recorded rather than refused — a foreman who opens a shift at five "
			"and calls it off at six before anybody arrives is the ordinary case."
		)
	events = described.get("compliance_events") or []
	if events:
		data["timeline_note"] = (
			f"{len(events)} event(s) are on this cancelled shift's timeline. They are KEPT: a water "
			"break called before the crew was stood down happened, and a cancellation does not "
			"unhappen it. But a timeline on a day nobody worked is worth reading — if the crew was "
			"out long enough to need water, they may be owed the hours."
		)
	return ToolResult(
		data=data,
		summary=f"cancelled {row['name']} at {when}: {reason}",
		docstatus_delta="0 → 0 (cancelled)",
	)


# ── 5. end_shift ────────────────────────────────────────────────────────────
def end_shift(args: dict) -> ToolResult:
	"""Close a shift with the supervisor's signature, and write the crew's payroll rows.

	THE SIGNATURE IS REQUIRED AND IT IS WHY THIS IS A TOOL. An unsigned close is
	an UPDATE setting a timestamp; the signature is what makes it the attestation
	FSMA §112.161(b) asks for — a review that is dated and signed rather than
	merely recorded.
	"""
	_require()
	actor = employee_tool.require_shift_role()
	row = _resolve_shift_for_update(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))
	if not shifts.is_open(row):
		raise ToolError(
			f"{row['name']} was already closed at {row.get('end_datetime')} by a review signed on "
			f"{row.get('supervisor_review_on') or 'an unrecorded date'}. Closing it again would "
			"write a second set of Attendance rows for the same day. Nothing was changed."
		)

	signature = file_reference(
		as_str(args, "supervisor_signature_file_token") or as_str(args, "supervisor_signature"),
		"supervisor_signature_file_token",
	)
	if not signature:
		raise ToolError(
			"supervisor_signature_file_token is required to close a shift. FSMA §112.161(b) asks "
			"for a review that is dated AND SIGNED by a supervisor or responsible party, and an "
			"unsigned close is an update statement setting a timestamp — there is nobody's name "
			"against what this shift claims happened. Upload the signature with stage_file_chunk "
			"and commit_staged_file, then pass its File docname. THE SHIFT IS STILL OPEN and "
			"nothing was changed."
		)

	end = _when(args, "end_datetime")
	if shifts.to_the_second(end) < shifts.to_the_second(row.get("start_datetime") or ""):
		raise ToolError(
			f"this call ends the shift at {end} and it started at {row.get('start_datetime')} — it "
			"would have finished before it began, and every crew member's Attendance row would "
			"carry a negative span. Nothing was changed."
		)

	crew_before = shifts.crew_of(row["name"])
	late = [
		entry
		for entry in crew_before
		if entry.get("left_at") and shifts.to_the_second(entry["left_at"]) > shifts.to_the_second(end)
	]
	if late:
		names = ", ".join(str(entry.get("employee_name") or entry.get("employee")) for entry in late)
		raise ToolError(
			f"{names} is recorded as leaving after {end}, which is when this call ends the shift. "
			"Nobody is on a shift that is over. Correct the departure time with "
			"remove_worker_from_shift, or end the shift later. Nothing was changed."
		)

	doc = frappe.get_doc(DOCTYPE, row["name"])
	doc.end_datetime = end
	doc.supervisor_review_signature = signature
	doc.supervisor_review_on = as_str(args, "reviewed_on") or frappe.utils.now()
	if as_str(args, "foreman_notes"):
		doc.foreman_notes = as_str(args, "foreman_notes")
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	closed = dict(doc.as_dict())
	described = shifts.describe(closed, with_children=True)
	# THE BRIDGE RUNS AFTER THE CLOSE IS WRITTEN AND CANNOT UNDO IT. See
	# `shifts.bridge_to_attendance`: the signature is the compliance act and the
	# payroll row is the convenience, so a site with no Frappe HR, an archived
	# employee, or a day somebody already keyed in by hand all produce a reported
	# skip rather than a shift that will not close.
	bridge = shifts.bridge_to_attendance(closed, shifts.crew_of(row["name"]))

	hours = shifts.hours_between(str(row.get("start_datetime") or ""), end)

	# ── S12. THE HOURS A MINOR ACTUALLY WORKED, checked at the moment they stop
	# being a plan and become a fact. ──────────────────────────────────────────
	#
	# `add_worker_to_shift` refuses a roster that WOULD breach a ceiling and
	# `start_shift` reports one, and until now nothing looked again. Both of those
	# run against a shift with no end time, so `minor_findings` projects nothing
	# and the day's total is whatever had already been worked elsewhere — which
	# means the ordinary case, one shift that simply RAN LONG, was never checked
	# by anything. A fifteen-year-old added to an empty roster at 07:00 and
	# clocked out at 19:30 passed every check this app made.
	#
	# REPORTED, NOT REFUSED, and here the argument is stronger than it is at
	# `start_shift`. The hours are already worked. Refusing the close would leave
	# the shift OPEN — no supervisor signature, no Attendance rows, no closed
	# compliance record — as a punishment for something the refusal cannot undo,
	# and the one document an investigator would ask for is the one that would not
	# exist. So the close goes through and carries the finding, which is what a
	# record is for.
	#
	# THE END TIME IS PASSED AS THE SHIFT'S OWN, so the projection is the real
	# span rather than nothing: `minor_findings` reads `end_datetime` off the row
	# it is given, and the row it is given here is the closed one.
	closed_row = {**row, "end_datetime": end}
	minors_found = [
		minor_findings(
			entry["employee"],
			str(entry.get("employee_name") or ""),
			closed_row,
			entry.get("joined_at") or str(row.get("start_datetime") or ""),
			exclude=row["name"],
		)
		for entry in crew_before
	]
	over = [entry for entry in minors_found if entry.get("blocked")]
	on_crew = [entry for entry in minors_found if entry.get("is_minor")]
	crowded = [entry for entry in minors_found if entry.get("days_warning")]

	data = {
		**described,
		"actor": actor,
		"shift_hours": hours,
		"attendance": bridge,
		"attendance_created": len(bridge.get("created") or []),
		"note": (
			f"{row['name']} is CLOSED, reviewed and signed. "
			f"{len(bridge.get('created') or [])} Attendance record(s) were written, one per crew "
			"member, each spanning that person's own joined_at to their own left_at — not the "
			"shift's span, because a worker who arrived late and left early did not work the "
			"whole day and a row claiming they did is wrong in the employer's favour."
		),
		"next_step": (
			"create_heat_exposure_event documents OAR 437-004-1131 for this shift where the day "
			"was hot. It is the record an inspector asks for by shift, and there is at most one "
			"per shift."
		),
	}
	# v0.85.0. THE CLOSE GOES UP THE CHAIN, FROZEN, AND CANNOT UNDO THE CLOSE.
	# Same contract as the Attendance bridge two blocks above and for the same
	# reason: the supervisor's signature is the compliance act, and neither a
	# payroll row nor a supervisory copy gets to fail it. `propagate` never
	# raises; what it could not do is reported here rather than thrown.
	#
	# THE SUBJECT IS THE FOREMAN, not the crew. A shift is the foreman's record —
	# they signed it — so the chain walked is theirs. A copy per crew member
	# would put one afternoon in front of a manager fourteen times.
	shadow = shadow_log.quiet_propagate(
		event_type=shadow_log.EVENT_SHIFT_CLOSED,
		source_doctype=DOCTYPE,
		source_name=row["name"],
		subject_employee=str(row.get("foreman") or ""),
		company=str(row.get("company") or ""),
		occurred_at=str(end),
		summary=(
			f"{described.get('foreman_name') or row.get('foreman') or 'A foreman'} closed "
			f"{row['name']}"
			+ (f" after {hours} hour(s)" if hours is not None else "")
			+ f" with {len(described.get('crew') or [])} crew member(s) and "
			f"{len(described.get('compliance_events') or [])} logged event(s)."
		),
		snapshot=shadow_log.snapshot_of(
			DOCTYPE,
			row["name"],
			{"_shift_hours": hours, "_attendance_created": len(bridge.get("created") or [])},
		),
	)
	if shadow:
		data["shadow_log"] = shadow

	if on_crew:
		data["minor_crew_findings"] = on_crew
	if crowded:
		data["minor_days_warnings"] = [entry["days_warning"] for entry in crowded]
	if over:
		data["minor_limits_exceeded"] = [
			{"employee": entry["employee"], "reasons": entry["blocked"]} for entry in over
		]
		notes = [entry["work_state_note"] for entry in over if entry.get("work_state_note")]
		data["minor_note"] = (
			f"THIS SHIFT CLOSES OVER A CHILD-LABOUR LIMIT for {len(over)} worker(s): "
			+ "; ".join(reason for entry in over for reason in entry["blocked"])
			+ ". The close was NOT refused — the hours are already worked, and an open shift with "
			"no supervisor signature would be a worse record of the same afternoon, not a "
			"correction of it. This is the finding on the record. If an end time is wrong, it is "
			"the end time that should be corrected; if it is right, this is a breach that "
			"happened and the next roster is where it gets prevented." + (f" {notes[0]}" if notes else "")
		)

	if not described["compliance_events"]:
		data["timeline_note"] = (
			"NOTHING WAS LOGGED ON THIS SHIFT'S TIMELINE. That is recorded rather than refused — a "
			"shift where nothing needed logging is a real shift — but on a hot day it is the "
			"absence an inspector reads as 'no water breaks were called', because there is no "
			"other reading available. log_shift_event is the record; it cannot be added "
			"convincingly afterwards."
		)
	return ToolResult(
		data=data,
		summary=(
			f"closed {row['name']} at {end}"
			+ (f" after {hours} hour(s)" if hours is not None else "")
			+ f"; {len(bridge.get('created') or [])} Attendance record(s) written"
		),
		docstatus_delta="0 → 0 (closed)",
	)


# ── 6. list_shifts ──────────────────────────────────────────────────────────
def list_shifts(args: dict) -> ToolResult:
	"""The shift register, filtered the four ways a question about a period asks."""
	_require()
	actor = employee_tool.require_shift_role()
	limit = min(as_limit(args), RECORD_CAP)

	filters = {}
	company = resolve_company(as_str(args, "company"), required=False)
	if company:
		employee_tool.require_company_scope(actor, company)
		filters["company"] = company
	else:
		allowed = _readable_companies(actor)
		if allowed:
			filters["company"] = ("in", allowed)

	foreman = as_str(args, "foreman")
	if foreman:
		filters["foreman"] = employee_tool.resolve_employee(foreman)

	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters["start_datetime"] = ("between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"])
	elif from_date:
		filters["start_datetime"] = (">=", f"{from_date} 00:00:00")
	elif to_date:
		filters["start_datetime"] = ("<=", f"{to_date} 23:59:59")

	shift_type = as_str(args, "shift_type")
	if shift_type:
		filters["shift_type"] = as_choice(DOCTYPE, "shift_type", shift_type, "shift_type")

	clock = timezones.Renderer(args)
	found = shifts.rows(filters, limit=max(limit * 2, limit))
	described = [shifts.describe(row, clock=clock) for row in found]

	status = as_str(args, "status")
	if status:
		wanted = {
			option.lower(): option
			for option in (shifts.STATUS_ACTIVE, shifts.STATUS_CLOSED, shifts.STATUS_CANCELLED)
		}.get(status.strip().lower())
		if not wanted:
			raise ToolError(
				f"status {status!r} is not one of Active, Closed, Cancelled. Note this is COMPUTED "
				"from whether the shift has an end time rather than read off the stored column — a "
				"record saved in March holds March's answer, and an open shift is what the weather "
				"sweep walks."
			)
		described = [entry for entry in described if entry["status"] == wanted]

	if as_str(args, "employee"):
		# WALKED IN PYTHON, because the crew is a child table and a join would
		# return one shift row per crew row. Somebody asking "which shifts was Ana
		# on" wants shifts, not crew rows.
		person = employee_tool.resolve_employee(as_str(args, "employee"))
		described = [
			entry
			for entry in described
			if any(str(crew.get("employee")) == person for crew in shifts.crew_of(entry["name"]))
		]

	truncated = len(described) > limit
	described = described[:limit]
	open_now = [entry["name"] for entry in described if entry["open"]]
	unsigned = [
		entry["name"] for entry in described if not entry["open"] and not entry["supervisor_reviewed"]
	]

	data = {
		"company": company,
		"count": len(described),
		"limit": limit,
		"truncated": truncated,
		"shifts": described,
		"open": open_now,
		"closed_without_a_signature": unsigned,
		"note": (
			f"{len(open_now)} shift(s) still open."
			if open_now
			else "Nothing in this selection is still open."
		),
	}
	if unsigned:
		data["signature_note"] = (
			f"{len(unsigned)} closed shift(s) carry no supervisor signature. end_shift cannot "
			"produce one, so these were closed in the Desk or by an import — and FSMA §112.161(b) "
			"asks for a review that is dated and signed. A signature added now is dated now."
		)
	if truncated:
		data["truncation_note"] = (
			f"More than {limit} shift(s) matched and this is the first {limit}. Narrow by company, "
			"foreman or period before relying on the counts above."
		)
	data.update(clock.block())
	return ToolResult(
		data=data,
		summary=(
			f"{len(described)} shift(s)"
			+ (f" for {company}" if company else "")
			+ f"; {len(open_now)} open, {len(unsigned)} closed unsigned"
		),
	)


# ── 7. get_shift ────────────────────────────────────────────────────────────
def get_shift(args: dict) -> ToolResult:
	"""One shift in full: the crew and their spans, the timeline, the weather, the heat record."""
	_require()
	actor = employee_tool.require_shift_role()
	row = _resolve_shift(args, "name")
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	clock = timezones.Renderer(args)
	described = shifts.describe(row, with_children=True, clock=clock)
	described.update(clock.block())
	heat = shifts.heat_rows({"farm_shift": row["name"]}, limit=2)

	data = {
		**described,
		"heat_exposure_event": (shifts.describe_heat_event(heat[0]) if heat else None),
		"crew_note": _crew_note(described),
		# A COUNT AND NOT THE TRACK. A shift with a fix every two minutes carries
		# hundreds of points, and returning them here would make every read of
		# every shift pay for a map nobody asked to see. `get_shift_track` is the
		# tool that draws it.
		"location_log_count": (
			frappe.db.count(shifts.LOCATION_DOCTYPE, {"shift": row["name"]})
			if compat.doctype_exists(shifts.LOCATION_DOCTYPE)
			else 0
		),
		# v0.64.0. THE OTHER HALF OF THE TASK↔SHIFT JOIN. The shift's own
		# `compliance_events` already carry a Task Completed entry per finished
		# job; this is the work that is still OPEN on the crew that is out there,
		# which no event can be because it has not happened yet.
		"farm_tasks": _tasks_on_shift(row["name"]),
	}
	if not described["weather_timeline"]:
		data["weather_note"] = (
			"NO WEATHER TIMELINE. Nothing populates it in v0.19.3 — the table ships so its shape "
			"is fixed and the compliance-event snapshots have somewhere to come from, and v0.19.4 "
			"wires the Open-Meteo fetch for open shifts plus archive backfill for closed ones. "
			"Until then a heat record's maxima are entered by hand."
		)
	if described["open"]:
		data["open_note"] = (
			"This shift is still open. Its compliance events are still being written and its "
			"Attendance rows do not exist yet — end_shift produces both."
		)
	elif not described["supervisor_reviewed"]:
		data["signature_note"] = (
			"This shift is closed and carries no supervisor signature, so it was not closed by "
			"end_shift. FSMA §112.161(b) asks for a review dated and signed by a supervisor, and "
			"there is nobody's name against what this shift says happened."
		)
	if not heat and described["shift_type"] in ("Harvest", "Prune", "Spray", "Irrigation"):
		data["heat_note"] = (
			"No Heat Exposure Event documents this shift. That is correct for a shift that never "
			f"reached a {shifts.HEAT_THRESHOLD_F:.0f} °F heat index and a gap for one that did — "
			"create_heat_exposure_event files it, and there is at most one per shift."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{row['name']} — {described['shift_type']} at "
			f"{described['location'] or 'an unnamed location'} under {described['foreman_name']}, "
			f"{described['status']}, {described['crew_size']} on the crew, "
			f"{described['compliance_event_count']} event(s) logged"
		),
	)


#: The Farm Task doctype, named here rather than imported so that the shift
#: surface keeps loading on a site without the dispatch tables. Every read goes
#: through `compat.doctype_exists` first.
FARM_TASK = "Farm Task"


def _tasks_on_shift(shift: str) -> dict:
	"""The tasks anchored to one shift, split by whether they are still open.

	v0.64.0. A COUNT AND A SHORT LIST, NOT THE BOARD. `get_shift` is already the
	longest read on this surface and a harvest shift can carry dozens of jobs;
	`list_dispatch_board(farm_shift=…)` is the tool that draws them in full. What
	belongs here is the answer to "is there work still open on this crew", which
	is one number and the names behind it.

	NEVER RAISES. A shift record is read in a wage claim and an inspection, and
	a dispatch table that is missing or mid-migrate is not a reason to refuse it.
	"""
	out = {"total": 0, "open": [], "completed": 0}
	if not compat.doctype_exists(FARM_TASK):
		return out
	try:
		rows = frappe.db.get_all(
			FARM_TASK,
			filters={"farm_shift": shift},
			fields=compat.existing_fields(
				FARM_TASK, ("name", "task_name", "task_type", "state", "urgency", "assigned_to_name")
			),
			order_by="modified desc",
			limit=200,
		)
	except Exception:
		return out
	terminal = ("Completed", "Cancelled", "Rejected")
	out["total"] = len(rows or [])
	out["completed"] = len([entry for entry in rows or [] if str(entry.get("state") or "") == "Completed"])
	out["open"] = [
		{
			"name": entry.get("name"),
			"task_name": entry.get("task_name"),
			"task_type": entry.get("task_type"),
			"state": entry.get("state"),
			"urgency": entry.get("urgency"),
			"assigned_to_name": entry.get("assigned_to_name") or None,
		}
		for entry in rows or []
		if str(entry.get("state") or "") not in terminal
	]
	return out


# ── 8. log_shift_location ───────────────────────────────────────────────────
def log_shift_location(args: dict) -> ToolResult:
	"""Append one GPS fix to a shift's track. Appends only; never edits.

	v0.32.0. THIS IS THE ONE TOOL ON THE SHIFT SURFACE THE WORKER'S PHONE DRIVES
	RATHER THAN THE FOREMAN, and it is not a contradiction of the sole-actor rule
	in `erpnext_mcp/shifts.py`. That rule is about who is ANSWERABLE — who forms
	the crew, calls the water break and signs the close — and none of that moves.
	A breadcrumb asserts nothing and attests to nothing. It records where a device
	was, which is a measurement rather than a claim, and the foreman's record is
	the thing it corroborates.

	ONE FIX PER CALL, in the shape a phone actually sends. A caller catching up
	after a canyon posts them in a loop; each one carries its own `timestamp`, so
	the order they arrive in does not matter and the track is drawn from when they
	were TAKEN.

	AN OPEN SHIFT IS NOT REQUIRED, and that is deliberate rather than lax. A phone
	that could not reach the site until the evening is posting fixes about a shift
	the foreman has already closed, and refusing them would throw away exactly the
	evidence that is hardest to collect. A fix outside the shift's own span is
	REPORTED — it is worth knowing about, because a phone left running in a truck
	after the crew went home traces the drive to the shop.
	"""
	_require()
	compat.require_doctype(
		shifts.LOCATION_DOCTYPE,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)
	actor = employee_tool.require_shift_role()
	row = _resolve_shift(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	latitude, longitude = _coordinates(args)
	when = _when(args, "timestamp")
	accuracy = args.get("accuracy_meters")
	source = as_choice(shifts.LOCATION_DOCTYPE, "source", as_str(args, "source") or "iOS", "source")

	person = as_str(args, "employee")
	if person:
		person = employee_tool.resolve_employee(person)
		theirs = str(frappe.db.get_value("Employee", person, "company") or "")
		if theirs and theirs != str(row.get("company") or ""):
			raise ToolError(
				f"{person} is employed by {theirs} and this shift belongs to {row.get('company')}. "
				"A breadcrumb filed against another entity's crew is evidence in the wrong packet. "
				"Nothing was created."
			)

	doc = frappe.new_doc(shifts.LOCATION_DOCTYPE)
	doc.shift = row["name"]
	doc.employee = person or None
	doc.timestamp = when
	doc.latitude = latitude
	doc.longitude = longitude
	doc.source = source
	if accuracy not in (None, ""):
		doc.accuracy_meters = as_float(accuracy, "accuracy_meters")
	doc.notes = as_str(args, "notes")
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	described = shifts.describe_location_row(dict(doc.as_dict()))
	warnings = []
	start = str(row.get("start_datetime") or "")
	end = str(row.get("end_datetime") or "")
	if start and str(when) < start:
		warnings.append(
			f"This fix is timestamped {when}, before the shift started at {start}. It is kept — the "
			"phone's clock is the phone's clock — but a track that begins before the crew does is "
			"usually a device left running from the day before."
		)
	if end and str(when) > end:
		warnings.append(
			f"This fix is timestamped {when}, after the shift ended at {end}. Kept for the same "
			"reason, and worth a look: a phone still reporting after the crew went home traces the "
			"drive to the shop rather than the work."
		)
	if described["accuracy_meters"] and described["accuracy_meters"] > POOR_ACCURACY_METRES:
		warnings.append(
			f"The phone reported {described['accuracy_meters']} m of accuracy on this fix, which is "
			f"past {POOR_ACCURACY_METRES:.0f} m. It is kept rather than dropped — a fix under a "
			"canopy in a canyon reads badly and is still the only record the crew was there — but "
			"it will not settle which side of a block line somebody was on."
		)

	total = frappe.db.count(shifts.LOCATION_DOCTYPE, {"shift": row["name"]})
	data = {
		"shift": row["name"],
		"company": row.get("company"),
		"log": doc.name,
		**described,
		"shift_open": shifts.is_open(row),
		"logs_on_this_shift": total,
		"warnings": warnings,
	}
	if not described["h3_cell"]:
		data["index_note"] = (
			"No H3 cell was computed for this fix, so it cannot be joined against a block's or a "
			f"parcel's stored coverage — this site is missing {geo.requires_sentence()}. The "
			"coordinates are stored and are the evidence; the cell is only the index over them."
		)
	return ToolResult(
		data=data,
		summary=(
			f"logged [{described['lat']}, {described['lon']}] at {described['timestamp']} on "
			f"{row['name']} ({total} fix(es) on this shift)"
		),
		docstatus_delta="none → 0 (created)",
	)


def _coordinates(args: dict) -> tuple:
	"""`latitude` and `longitude` from the call, both required and both on Earth.

	DELEGATED TO `geo.coordinates` SINCE THE TRAINING SESSION NEEDED THE SAME
	PAIR. This was written inline here and copied an hour later, which is two
	chances for one surface to accept the pair the wrong way round; the aliases,
	the range check and the all-or-nothing rule now live in one function and both
	callers get the same refusal. The wrapper stays because "a breadcrumb with no
	position is a timestamp" is a sentence about a SHIFT TRACK, and the shared
	function has no business knowing what its caller is recording.
	"""
	return geo.coordinates(args, required=True, tail="Nothing was created.")


# ── 9. get_shift_track ──────────────────────────────────────────────────────
def get_shift_track(args: dict) -> ToolResult:
	"""Where the crew went during one shift, in the order it happened.

	IN THE ORDER THE FIXES WERE TAKEN, not the order they arrived. A phone out of
	signal posts an hour of breadcrumbs the moment the bars come back, and a track
	sorted by insertion draws the crew standing still all morning where the signal
	returned and then teleporting across the farm.

	THE GAPS ARE REPORTED because a track's silences are the part a reader
	misjudges. Twenty minutes with no fix is a phone in a pocket under a canopy,
	an hour is a device that was off, and a straight line drawn between the two
	ends of either one is a line the crew did not walk.
	"""
	_require()
	actor = employee_tool.require_shift_role()
	row = _resolve_shift(args, "shift")
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	person = as_str(args, "employee")
	if person:
		person = employee_tool.resolve_employee(person)

	# NOT `as_limit`, whose 500-row ceiling is the right one for a register and the
	# wrong one for a track: a nine-hour shift at a fix every thirty seconds is
	# eleven hundred points, and a track cut at five hundred loses the afternoon
	# without saying which half went.
	limit = max(1, min(TRACK_CAP, as_int(args, "limit", TRACK_CAP)))
	found = shifts.track_of(row["name"], person, limit=limit + 1)
	truncated = len(found) > limit
	found = found[:limit]
	points = [shifts.describe_location_row(entry) for entry in found]

	first = points[0]["timestamp"] if points else None
	last = points[-1]["timestamp"] if points else None
	data = {
		"shift": row["name"],
		"company": row.get("company"),
		"employee": person or None,
		"shift_start": str(row.get("start_datetime") or "") or None,
		"shift_end": str(row.get("end_datetime") or "") or None,
		"shift_open": shifts.is_open(row),
		"count": len(points),
		"truncated": truncated,
		"first_fix": first,
		"last_fix": last,
		"track": points,
		"gaps": _track_gaps(points),
		"employees_tracked": sorted({entry["employee"] for entry in points if entry["employee"]}),
	}
	if not points:
		data["note"] = (
			"No location logs for this shift. That is the ordinary case for a shift worked before "
			"the phones were logging, and it is not a gap in the compliance record — the shift's "
			"own location, crew spans and event timeline are unaffected. log_shift_location is what "
			"writes a track, and it is off by default."
		)
	if truncated:
		data["truncation_note"] = (
			f"More than {limit} fix(es) are on this shift and this is the first {limit} by time. "
			"Narrow by employee, or read the register directly — the ones after the cut are the END "
			"of the day, which is the half a truncated track quietly loses."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{row['name']}: {len(points)} fix(es)"
			+ (f" from {first} to {last}" if points else "")
			+ (f" for {person}" if person else "")
		),
	)


def _track_gaps(points: list) -> list:
	"""Every silence longer than `TRACK_GAP_SECONDS`, as it would be drawn.

	Reported rather than filled. The alternative — interpolating between the two
	ends — invents a position for every minute the phone was quiet, and an
	invented position on a record read in a wage dispute or a re-entry-interval
	question is the worst thing this app could put on a map.
	"""
	out = []
	for earlier, later in itertools.pairwise(points):
		if not earlier["timestamp"] or not later["timestamp"]:
			continue
		try:
			seconds = float(frappe.utils.time_diff_in_seconds(later["timestamp"], earlier["timestamp"]))
		except Exception:  # pragma: no cover - an unparseable stored timestamp
			continue
		if seconds > TRACK_GAP_SECONDS:
			out.append(
				{
					"from": earlier["timestamp"],
					"to": later["timestamp"],
					"minutes": round(seconds / 60.0, 1),
				}
			)
	return out


# ── 9. log_shift_break ────────────────────────────────────────────────────

#: The payroll classification a break is logged under, and the compliance event
#: type each one produces. `event_type` is DERIVED from this map and is never
#: taken from a caller's body — see `log_shift_break`.
#:
#: v0.96.0 ADDED WATER BREAK AND SHADE BREAK, and they are not decoration on
#: Cool-Down. OAR 437-004-1131 and WAC 296-307-097 make drinking water, shade
#: and a cool-down rest period THREE separately required provisions, and the
#: question an inspector asks after a heat event is whether SHADE was provided —
#: a register that recorded all three as "Cool-Down" cannot answer it. Both event
#: types have been on the Farm Shift Compliance Event doctype since it shipped
#: (`CARE_EVENTS` has counted them per worker since v0.64.0); what was missing was
#: the payroll classification that lets `log_shift_break` reach them, so a handset
#: sending `Water Break` was refused and the break went unlogged. For PAYROLL all
#: three are paid rest, which is why they share `paid: True` — the distinction
#: they carry is a compliance one.
BREAK_KINDS = {
	"Paid Rest": {"event_type": "Rest Period", "paid": True},
	"Unpaid Meal": {"event_type": "Meal Period", "paid": False},
	"Cool-Down": {"event_type": "Cool-Down", "paid": True},
	"Water Break": {"event_type": "Water Break", "paid": True},
	"Shade Break": {"event_type": "Shade Break", "paid": True},
}

#: The shorter spellings a caller may send, mapped onto the value the Select
#: column actually holds.
#:
#: THE HANDSET'S ENUM SPELLS THE TWO HEAT PROVISIONS `Water` AND `Shade`. That is
#: `BreakEvent.Kind`'s rawValue in `FarmOpsKit`, and it is what a phone sends on
#: the day it stops folding them onto Cool-Down. v0.96.0 widened `BREAK_KINDS` to
#: `Water Break` and `Shade Break` — the doctype's own wording, which is NOT what
#: the app says — so item 9 of `SERVER_CHANGES.md` was left half closed: the app
#: reaches a widened list by comparing on letters and digits alone, and `water`
#: never matches `waterbreak`. Accepting both spellings is what actually lets a
#: shade break arrive as a shade break.
#:
#: AN ALIAS IS THE SAME PROVISION SPELLED SHORTER AND NEVER A DIFFERENT ONE. An
#: unpaid meal must never resolve to a paid rest to make a write succeed — that
#: is a payroll record saying the wrong thing about somebody's day — so this map
#: holds only pairs whose two names are the same break.
BREAK_KIND_ALIASES = {
	"Water": "Water Break",
	"Shade": "Shade Break",
}


def _break_kind_spelling(value: str) -> str:
	"""The comparison form of a break kind: letters and digits, lower case.

	`Cool-Down`, `Cool Down`, `cooldown` and `COOL_DOWN` are ONE option spelled
	four ways, and an administrator who retyped the Select had no idea a handset
	was matching on it exactly. This is the same normalisation the app applies to
	the list it reads out of a refusal, deliberately: two systems agreeing about
	which spellings are the same word is the whole point.
	"""
	return "".join(character for character in value.lower() if character.isalnum())


#: Every spelling this tool accepts, mapped to the one it stores. Built from
#: `BREAK_KINDS` and `BREAK_KIND_ALIASES` rather than typed out, so a kind added
#: to either is reachable by both spellings without a third list to keep.
_BREAK_KIND_BY_SPELLING = {
	**{_break_kind_spelling(name): name for name in BREAK_KINDS},
	**{_break_kind_spelling(alias): name for alias, name in BREAK_KIND_ALIASES.items()},
}


def canonical_break_kind(value: str) -> str:
	"""What a caller's `break_kind` is stored as, or "" when it is not a break kind.

	Empty rather than an exception: the caller raises the refusal, because the
	sentence it raises is one the handset PARSES — `BreakKindRefusal` reads the
	accepted list out of it and retries — and that sentence belongs next to the
	call it refuses.
	"""
	return _BREAK_KIND_BY_SPELLING.get(_break_kind_spelling(value), "")


VALID_APPLIES_TO = ("Crew", "Individual")


def _push_break(shift_name: str, break_kind: str, phase: str, duration_minutes=None, event: str = "") -> dict:
	"""Ring the crew's phones for a break that covers the crew. Never raises.

	WHY THIS IS HERE AND NOT IN THE HANDSET. `BreakAlarm` plays the tone the
	moment a foreman calls a break, over an audio session that rings through the
	silent switch — on exactly one phone, the one the break was called on. Every
	other worker on the shift found out when somebody shouted. The push is how
	the other twenty phones hear it, and the app plays the delivered tone through
	the same code path as the local one.

	INDIVIDUAL BREAKS ARE NOT PUSHED. A break that covers one named worker is not
	news to the other nineteen, and a tone that rings through a silent switch is
	not a thing to send to somebody it is not about. The caller decides; this
	function is only called when `applies_to` is Crew.

	A FAILURE HERE IS NEVER A FAILURE OF THE BREAK LOG. The break record is the
	compliance evidence under OAR 437-004-1131 and the push is a convenience on
	top of it; a site with no APNs key, no network, or no enrolled handsets must
	log the break exactly as it did before. So the report is returned for the
	caller to put on its answer, and nothing in it is ever raised.
	"""
	try:
		payload = push_service.break_payload(
			break_kind=break_kind,
			phase=phase,
			duration_minutes=duration_minutes,
			shift=shift_name,
			event=event,
		)
		return push_service.send_push_to_shift_crew(shift_name, payload)
	except Exception as error:  # pragma: no cover - send_push_to_shift_crew is itself wrapped
		return {"shift": shift_name, "sent": 0, "failed": 0, "skipped": 0, "reason": f"error: {error}"}


def log_shift_break(args: dict) -> ToolResult:
	"""Start a break on a shift — rest, meal or cool-down.

	A thin, opinionated wrapper over `log_shift_event`. Validates the break-
	specific fields together: an Individual break must name an employee, a Crew
	break must not, and the break_kind must be one of the three payroll-meaningful
	values.
	"""
	_require()
	actor = employee_tool.require_shift_role()
	row = _resolve_shift_for_update(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	sent_kind = as_str(args, "break_kind", required=True).strip()
	# RESOLVED BEFORE IT IS VALIDATED, AND STORED CANONICAL. A body may spell a
	# heat break the way the handset's enum does (`Water`, `Shade`) or the way an
	# administrator retyped the column (`Cool Down`); the Select holds one
	# spelling, and every reader in `breaks.py` — `HEAT_RELIEF_KINDS`, the payroll
	# tallies, `next_break_due` — compares against it BY NAME. Storing what
	# arrived would make those comparisons miss and the break vanish from the
	# counts it exists to feed.
	break_kind = canonical_break_kind(sent_kind)
	if not break_kind:
		# The refusal still enumerates the CANONICAL values, and not the aliases
		# beside them: it is read by a machine as well as a person, and the list
		# it hands back is the list a retry is built from.
		raise ToolError(f"break_kind must be one of {', '.join(BREAK_KINDS)}. Got {sent_kind!r}.")

	applies_to = (as_str(args, "applies_to") or "Crew").strip()
	if applies_to not in VALID_APPLIES_TO:
		raise ToolError(f"applies_to must be Crew or Individual. Got {applies_to!r}.")

	employee = None
	if applies_to == "Individual":
		employee = employee_tool.resolve_employee(as_str(args, "employee", required=True))
	elif as_str(args, "employee"):
		raise ToolError(
			"employee is set but applies_to is Crew. A crew break covers everybody on the "
			"shift — remove employee or set applies_to to Individual."
		)

	when = _when(args, "started_at")
	# v0.64.0. `as_float` takes the VALUE first and the label second — every other
	# one of its hundred-odd call sites does — and this one passed the whole args
	# dict as the value. `float({...})` raises, so this line failed EVERY call to
	# this tool since v0.58.0, and the refusal it produced quoted the entire
	# request payload back as the offending "number".
	duration = as_float(args.get("duration_minutes"), "duration_minutes")
	event_type = BREAK_KINDS[break_kind]["event_type"]

	# v0.98.0. A HEAT BREAK IS A COMPLIANCE EVENT AND NOT ONLY A PAYROLL ONE, and
	# what makes it one is the weather beside it. `break_kind` and `event_type`
	# already told the register WHICH provision was discharged; what they could
	# not say is what the crew was standing in when it was — and OAR
	# 437-004-1131's obligations attach at a heat-index CROSSING, not at the
	# moment relief happens. So the three heat kinds carry the shift's peak
	# figures, the crossing timestamp and the provenance of the reading, copied
	# from the timeline at the instant they are logged.
	#
	# DERIVED FROM `break_kind` AND NOT TAKEN FROM THE BODY. `heat_obligation` is
	# the column that decides whether a break counts toward a heat-illness
	# obligation, and a phone that could set it directly could file an unpaid meal
	# as a cool-down in the one register that has to answer honestly — the same
	# argument that keeps `event_type` off this method's signature.
	heat = break_kind in breaks_mod.HEAT_RELIEF_KINDS
	entry = {
		"event_type": event_type,
		"event_datetime": when,
		"logged_by": row.get("foreman"),
		"description": as_str(args, "description") or None,
		"break_kind": break_kind,
		"duration_minutes": duration,
		"duration_source": "Scheduled",
		"applies_to": applies_to,
		"employee": employee,
		"heat_obligation": 1 if heat else 0,
	}
	if heat:
		# EMPTY KEYS ARE DROPPED rather than written as nulls. A shift with no
		# weather timeline — no GPS on the shift, or the sweep has not run — gets
		# a break row with blank heat columns, which is the honest answer:
		# nobody measured, and that is not a temperature.
		entry.update(
			{
				key: value
				for key, value in shifts.heat_conditions(shifts.weather_of(row["name"]), when).items()
				if value is not None
			}
		)

	doc = frappe.get_doc(DOCTYPE, row["name"])
	appended = doc.append("compliance_events", entry)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	described = shifts.describe(dict(doc.as_dict()), with_children=True)
	crew_on_shift = described.get("still_on_shift") or described.get("crew_size") or 0
	covers = crew_on_shift if applies_to == "Crew" else 1

	break_tally = {}
	for ev in described.get("compliance_events") or []:
		bk = ev.get("break_kind")
		if bk:
			break_tally[bk] = break_tally.get(bk, 0) + 1

	# The break horn. Only for a break that covers the crew — an Individual
	# break is not news to the other nineteen phones. See `_push_break`.
	# The row's OWN docname, taken from the object `append` handed back rather
	# than found again by matching on datetime and kind. Two crew breaks logged
	# in the same second — a foreman double-tapping — are indistinguishable by
	# those two fields, and the handset uses this name to end the break.
	logged_event = str(getattr(appended, "name", "") or "")
	push_report = (
		_push_break(row["name"], break_kind, "start", duration, logged_event)
		if applies_to == "Crew"
		else {"reason": "individual_break", "sent": 0, "failed": 0, "skipped": 0}
	)

	data = {
		**described,
		"actor": actor,
		"logged": {
			"break_kind": break_kind,
			"started_at": when,
			"duration_minutes": duration,
			"applies_to": applies_to,
			"covers_workers": covers,
			"event": logged_event,
			"heat_obligation": heat,
		},
		"breaks_today": break_tally,
		"push": push_report,
	}
	if heat:
		data["heat_conditions"] = {
			key: entry.get(key)
			for key in ("peak_temp_f", "peak_heat_index_f", "threshold_crossed_at", "weather_source")
		}
		if entry.get("threshold_crossed_at"):
			data["heat_note"] = (
				f"This {break_kind} discharges a heat-illness-prevention obligation under "
				f"{shifts.CITATION}, not a wage-and-hour one, and the row carries the conditions "
				f"it was called in: the shift crossed {shifts.HEAT_THRESHOLD_F:.0f} °F heat index "
				f"at {entry['threshold_crossed_at']} and peaked at "
				f"{entry.get('peak_heat_index_f')} °F. The interval between the crossing and this "
				"break is what an inspector reads off one row instead of reconstructing from "
				"ninety weather readings."
			)
		elif not entry.get("peak_heat_index_f"):
			data["heat_note"] = (
				f"This {break_kind} is logged as a heat-obligation break, but {row['name']} has "
				"no weather reading at or before it — so the row carries no conditions. Blank "
				"rather than zero: nobody measured, which is not a temperature. A shift with no "
				"farm_location_gps gets no timeline; set it while the shift is open."
			)
	return ToolResult(
		data=data,
		summary=f"logged {break_kind} on {row['name']} at {when} ({applies_to})",
		docstatus_delta="0 → 0 (amended)",
	)


# ── 10. end_shift_break ──────────────────────────────────────────────────


def end_shift_break(args: dict) -> ToolResult:
	"""End a running break — write the observed duration.

	Writes `ended_at` and the true `duration_minutes`, and flips
	`duration_source` to Observed.
	"""
	_require()
	actor = employee_tool.require_shift_role()
	row = _resolve_shift_for_update(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	event_name = as_str(args, "event", required=True).strip()
	ended_at = _when(args, "ended_at")

	doc = frappe.get_doc(DOCTYPE, row["name"])
	target = None
	for entry in doc.compliance_events or []:
		if str(entry.name) == event_name:
			target = entry
			break
	if target is None:
		raise ToolError(
			f"No compliance event {event_name!r} on {row['name']}. "
			"log_shift_break or log_shift_event creates one first."
		)
	if not target.get("break_kind"):
		raise ToolError(
			f"Event {event_name} is a {target.get('event_type')}, not a break event. "
			"Only break events (with a break_kind) can be ended."
		)

	start_str = str(target.get("event_datetime") or "")
	if ended_at < start_str:
		raise ToolError(
			f"ended_at ({ended_at}) is before the break started ({start_str}). "
			"A break cannot end before it began."
		)

	target.ended_at = ended_at
	observed_minutes = shifts.hours_between(start_str, ended_at)
	if observed_minutes is not None:
		target.duration_minutes = round(observed_minutes * 60, 1)
	target.duration_source = "Observed"

	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	described = shifts.describe(dict(doc.as_dict()), with_children=True)

	break_tally = {}
	for ev in described.get("compliance_events") or []:
		bk = ev.get("break_kind")
		if bk:
			break_tally[bk] = break_tally.get(bk, 0) + 1

	# The end-of-break bell, on the same rule as the start: the crew's phones for
	# a crew break, and nobody's for an individual one. The app schedules its own
	# local `UNNotificationRequest` for the foreman's handset, so the foreman may
	# hear it twice — which is the right way round, since the alternative is
	# nineteen workers hearing it never.
	push_report = (
		_push_break(
			row["name"],
			str(target.get("break_kind") or "Break"),
			"end",
			target.duration_minutes,
			event_name,
		)
		if str(target.get("applies_to") or "Crew") == "Crew"
		else {"reason": "individual_break", "sent": 0, "failed": 0, "skipped": 0}
	)

	return ToolResult(
		data={
			**described,
			"actor": actor,
			"ended": {
				"event": event_name,
				"break_kind": target.get("break_kind"),
				"started_at": start_str,
				"ended_at": ended_at,
				"duration_minutes": target.duration_minutes,
				"duration_source": "Observed",
			},
			"breaks_today": break_tally,
			"push": push_report,
		},
		summary=f"ended {target.get('break_kind')} on {row['name']} at {ended_at}",
		docstatus_delta="0 → 0 (amended)",
	)


# ── 11. get_break_policy ─────────────────────────────────────────────────

BREAK_POLICY_DOCTYPE = "Labor Break Policy"


def get_break_policy(args: dict) -> ToolResult:
	"""The break schedule the handset counts its break coach from."""
	compat.require_doctype(
		BREAK_POLICY_DOCTYPE,
		"The Labor Break Policy DocType ships with erpnext_mcp v0.58.0 — run `bench migrate`.",
	)
	employee_tool.require_shift_role()
	# THE CALL USED TO BE `resolve_company(args, actor)`, WHICH IS NOT THIS
	# FUNCTION'S SIGNATURE. `args.resolve_company` takes (company, required) —
	# so the dict went in where the docname belongs, `(company or "").strip()`
	# raised AttributeError on it, and EVERY call to this endpoint answered
	# HTTP 500. A handset asking for a company's break policy is the ordinary
	# case, so the ordinary case was the one that crashed; the only bodies that
	# survived were the ones that named nothing at all.
	#
	# THE COMPANY IS VALIDATED AND NOT FILTERED ON, which is deliberate rather
	# than left over: `Labor Break Policy` has no company column. A policy is a
	# STATE's rule — OAR 437-004-1131 is Oregon's whoever employs you — so the
	# register is keyed on `work_state` and an entity filter would be a filter on
	# a column that is not there. Resolving it still earns its place: a body
	# naming a company this site does not have gets told so, by name, instead of
	# being handed another entity's schedule.
	company = resolve_company(as_str(args, "company"), required=False)
	work_state = as_str(args, "work_state") or ""

	filters = {"enabled": 1}
	if work_state:
		filters["work_state"] = work_state

	policies = frappe.db.get_all(
		BREAK_POLICY_DOCTYPE,
		filters=filters,
		fields=["name"],
		order_by="effective_from desc",
		limit_page_length=1,
	)
	if not policies:
		return ToolResult(
			data={
				"policy": None,
				"company": company,
				"note": "No enabled break policy found for this state.",
			},
			summary="no break policy found",
		)

	doc = frappe.get_doc(BREAK_POLICY_DOCTYPE, policies[0]["name"])
	policy_dict = _describe_break_policy(dict(doc.as_dict()))
	# The entity the body asked about, echoed back. The policy itself is the
	# state's and carries no company, so this is the one key that says which
	# farm the question was asked on behalf of.
	policy_dict["company"] = company
	if not policy_dict["has_minor_schedule"]:
		# STATED, NOT FILLED IN. `human_approved_by` exists on this doctype
		# because a break schedule is something the operation says about what it
		# owes its crew, so writing rows into an approved policy would move that
		# statement with nobody's name on it. The rows are handed back marked
		# unapproved instead: the gap is visible on the handset that is about to
		# coach a minor off the adult table, and the operator has the table to
		# paste rather than a citation to go and look up.
		policy_dict["minor_schedule_suggested"] = {
			"citation": minors.MINOR_SCHEDULE_CITATION,
			"approved": False,
			"rest_schedule": [dict(entry) for entry in minors.MINOR_REST_SCHEDULE],
			"meal_schedule": [dict(entry) for entry in minors.MINOR_MEAL_SCHEDULE],
		}
		policy_dict["minor_gap"] = (
			f"{doc.name} carries no minor rest or meal schedule, so a worker under eighteen on "
			f"a shift under this policy is being counted off the ADULT table — which owes fewer "
			f"periods, and is therefore a shortfall rather than an exemption. "
			f"`minor_schedule_suggested` is {minors.MINOR_SCHEDULE_CITATION} written as rows; "
			f"nothing wrote them here, because an approved policy is a statement somebody signed."
		)

	return ToolResult(
		data=policy_dict,
		summary=f"break policy {doc.name} for {doc.work_state}",
	)


#: How long a shift nobody has closed is assumed to run, in hours.
#:
#: THE ENTITLEMENT BANDS ARE KEYED ON THE LENGTH OF THE SHIFT, NOT ON HOURS
#: WORKED SO FAR, which is why an open shift needs an assumption rather than a
#: measurement: six hours owes a meal period and four does not, so computing
#: against elapsed time would make the meal APPEAR on the schedule at the
#: four-hour mark — three hours after the crew needed to know it was coming, and
#: with the countdown already showing it as overdue the moment it arrived.
#:
#: EIGHT BECAUSE THAT IS THE DAY THIS IS FOR, and a caller who knows better says
#: so: `planned_hours` overrides it, and a shift that has been CLOSED is measured
#: end to end and never guessed at. Eight is also the conservative direction —
#: it owes the meal period a seven-hour day owes and a five-hour day does not,
#: and a break offered and not needed costs ten minutes where one needed and not
#: offered is a wage claim.
PLANNED_SHIFT_HOURS = 8.0


def _break_schedule_for(row: dict, planned_hours=None, now: str = "", employee: str = "") -> dict:
	"""The timed break schedule for one shift. The computation, without the gates.

	Split from `get_break_schedule` below so the resolution, the role check and
	the company scope live in one place and this stays a function of a shift row
	— which is what lets `get_shift` grow a schedule later without either of them
	forking the arithmetic.

	THE POLICY IS THE SHIFT'S OWN FIRST AND THE STATE'S SECOND. A `Farm Shift`
	carries `break_policy`, stamped when the shift was started, and that is the
	one to honour: a policy amended in October must not retroactively change what
	August's crew was owed. Where the column is empty — every shift started
	before v0.58.0, and any started on a site that had no policy yet — the
	enabled policy for the shift's `work_state` is read the same way
	`get_break_policy` reads it, and the answer SAYS which of the two happened.
	An app that cannot tell "this is your farm's policy" from "this is the
	state's default" cannot print the sentence `BreakSchedule` prints.

	`employee` NAMES WHOSE SCHEDULE THIS IS, and it is optional because most of
	the time it is the crew's. A worker under eighteen is owed a rest every two
	hours and a meal every four (OAR 839-021-0072), so their countdown is a
	different set of instants from the crew's — and `BreakSchedule.compute(...,
	isMinor:)` on the handset has computed exactly that locally since before
	this endpoint existed. Naming them switches the tables and puts `is_minor`
	on the answer, which is what the purple badge reads. Named nobody, the
	answer is the adult schedule and says so.
	"""
	policy = _break_policy_dict(row)
	resolved_from = "shift" if row.get("break_policy") and policy else ""
	if not policy:
		fallback = _enabled_policy_for(str(row.get("work_state") or ""))
		if fallback:
			policy, resolved_from = fallback, "work_state"

	start = str(row.get("start_datetime") or "")
	end = str(row.get("end_datetime") or "")
	if planned_hours not in (None, ""):
		hours = as_float(planned_hours, "planned_hours")
	elif end:
		# A CLOSED SHIFT IS MEASURED, NEVER ASSUMED. Its length is a fact by
		# then, and the schedule this returns is what a payroll reconciliation
		# and an inspector read rather than what a countdown bar draws.
		hours = breaks_mod._hours_between(start, end)
	else:
		hours = PLANNED_SHIFT_HOURS

	readings = [dict(entry) for entry in (row.get("weather_timeline") or [])]
	if not readings and row.get("name"):
		readings = [dict(entry) for entry in (shifts.weather_of(str(row["name"])) or [])]
	heat_index = None
	measured = [
		breaks_mod._as_float(entry.get("heat_index_f"))
		for entry in readings
		if entry.get("heat_index_f") not in (None, "")
	]
	if measured:
		heat_index = max(measured)

	events = [
		dict(entry) for entry in shifts.events_of(str(row.get("name") or "")) if entry.get("break_kind")
	]
	# WHOSE SCHEDULE, ANSWERED AS OF THE SHIFT'S OWN DAY. Same one query
	# `shifts.describe` runs over the whole crew, for one person here.
	described = (
		shifts.minor_flags([employee], start)[employee]
		if employee
		else {"is_minor": None, "minor_band": None, "minor_limits": None}
	)
	rows = breaks_mod.schedule(
		start,
		policy,
		hours=hours,
		events=events,
		now=now or frappe.utils.now(),
		heat_index=heat_index,
		is_minor=described.get("is_minor"),
	)

	owed = [entry for entry in rows if not entry["taken"]]
	return {
		"shift": row.get("name"),
		"company": row.get("company") or None,
		"work_state": row.get("work_state") or None,
		"shift_start": start or None,
		"shift_end": end or None,
		"hours": round(hours, 2),
		"hours_are_planned": not end and planned_hours in (None, ""),
		"policy": policy.get("policy") if policy else None,
		"policy_source": resolved_from or None,
		"policy_approved": bool(policy.get("approved")) if policy else False,
		"regulation_citations": policy.get("regulation_citations") if policy else None,
		"heat_index": heat_index,
		"employee": employee or None,
		"is_minor": described.get("is_minor"),
		"minor_band": described.get("minor_band"),
		"schedule_band": (
			"minor" if any(entry.get("schedule_band") == "minor" for entry in rows) else "adult"
		),
		"breaks": rows,
		"count": len(rows),
		"outstanding": len(owed),
		"next_due": next((entry["due_at"] for entry in owed), None),
		# THE ONE SENTENCE THE HANDSET PRINTS UNDER THE COUNTDOWN. `BreakSchedule`
		# already prints which source it used, every time, because a countdown
		# that looks authoritative and is really the app's own reading of OAR
		# 839-020-0050 is the kind of thing somebody quotes in a wage claim. This
		# is that sentence written where the fact is.
		"note": _schedule_note(policy, resolved_from),
	}


def _schedule_note(policy: dict, resolved_from: str) -> str:
	if not policy:
		return (
			"No enabled break policy for this shift or its work state, so nothing is scheduled. "
			"The handset falls back to the statutory minimum it carries and must say so."
		)
	if resolved_from == "shift":
		return (
			f"Computed from {policy.get('policy')}, the policy stamped on this shift when it "
			"started. Amending the policy now does not change what this crew was owed."
		)
	return (
		f"This shift names no break policy, so {policy.get('policy')} — the enabled policy for "
		f"{policy.get('work_state') or 'this state'} — was used. Shifts started before v0.58.0 "
		"carry no stamp."
	)


def _enabled_policy_for(work_state: str) -> dict:
	"""The enabled policy for one state, as a dict, or `{}`.

	The same lookup `get_break_policy` makes, factored out rather than repeated,
	so the schedule and the policy read can never answer from different records.
	"""
	if not compat.doctype_exists(BREAK_POLICY_DOCTYPE):
		return {}
	filters = {"enabled": 1}
	if work_state:
		filters["work_state"] = work_state
	found = frappe.db.get_all(
		BREAK_POLICY_DOCTYPE,
		filters=filters,
		fields=["name"],
		order_by="effective_from desc",
		limit_page_length=1,
	)
	if not found:
		return {}
	try:
		return _describe_break_policy(dict(frappe.get_doc(BREAK_POLICY_DOCTYPE, found[0]["name"]).as_dict()))
	except Exception:  # pragma: no cover - deleted between the query and the read
		return {}


def get_break_schedule(args: dict) -> ToolResult:
	"""Every break one shift owes, and the clock time each falls due.

	v0.98.0, ITEM 14, AND THE POINT IS THAT ONE MACHINE COMPUTES IT. Breaks
	should be scheduled from the shift's start against the state's rules, not
	logged from memory when somebody remembers, and a schedule each handset works
	out for itself drifts by whatever the phones disagree about — the start time,
	the device clock, and which policy the app managed to fetch before it lost
	signal. This returns INSTANTS rather than durations, so seven phones on one
	crew show the same 10:05 whether they asked at the tailgate or an hour later.

	`get_break_policy` IS STILL THE OTHER HALF AND IS NOT REPLACED. That answers
	what the rules ARE — the bands, the citations, whether a human approved them
	— for a screen that shows the policy. This answers what THIS shift owes and
	when, which is a different question with a shift in it, and the handset needs
	both: the countdown, and the sentence under it saying where the countdown
	came from.
	"""
	_require()
	actor = employee_tool.require_shift_role()
	row = _resolve_shift(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	person = as_str(args, "employee")
	if person:
		person = employee_tool.resolve_employee(person)
	data = _break_schedule_for(
		row,
		planned_hours=args.get("planned_hours"),
		now=as_str(args, "now"),
		employee=person,
	)
	if data.get("is_minor") and data["schedule_band"] == "adult":
		data["minor_gap"] = (
			f"{person} is under eighteen and {data.get('policy') or 'this policy'} carries no "
			"minor rest or meal schedule, so this countdown is the ADULT one — which owes fewer "
			f"periods. {minors.MINOR_SCHEDULE_CITATION} is what it should be; get_break_policy "
			"hands back the rows to add."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{data['shift']}: {data['count']} break(s) scheduled, {data['outstanding']} outstanding"
			+ (f", next at {data['next_due']}" if data["next_due"] else "")
			+ (" (minor's schedule)" if data["schedule_band"] == "minor" else "")
		),
	)


def _describe_break_policy(row: dict) -> dict:
	approved_by = row.get("human_approved_by") or None
	return {
		"policy": row.get("name") or row.get("policy_id"),
		"work_state": row.get("work_state"),
		"effective_from": str(row.get("effective_from") or "") or None,
		"effective_to": str(row.get("effective_to") or "") or None,
		"approved": bool(approved_by),
		"approved_by": approved_by,
		"regulation_citations": row.get("regulation_citations") or None,
		"rest_schedule": [
			{
				"hours_from": r.get("hours_from"),
				"hours_to": r.get("hours_to"),
				"periods_owed": r.get("periods_owed"),
				"minutes_each": r.get("minutes_each"),
				"paid": bool(r.get("paid")),
			}
			for r in (row.get("rest_schedule") or [])
		],
		"meal_schedule": [
			{
				"hours_from": r.get("hours_from"),
				"hours_to": r.get("hours_to"),
				"periods_owed": r.get("periods_owed"),
				"minutes_each": r.get("minutes_each"),
				"paid": bool(r.get("paid")),
			}
			for r in (row.get("meal_schedule") or [])
		],
		"heat_schedule": [
			{
				"heat_index_from": r.get("heat_index_from"),
				"heat_index_to": r.get("heat_index_to"),
				"minutes_each": r.get("minutes_each"),
				"every_hours": r.get("every_hours"),
				"concurrent_with_rest": bool(r.get("concurrent_with_rest")),
			}
			for r in (row.get("heat_schedule") or [])
		],
		"max_hours_without_rest": row.get("max_hours_without_rest") or None,
		# v0.98.0. THE SCHEDULE A WORKER UNDER EIGHTEEN COUNTS FROM, and it is
		# returned whether or not it is filled in. iOS has computed a minor's
		# break schedule since `BreakSchedule.compute(..., isMinor:)` shipped and
		# had nothing to compute it from: the endpoint answered with one table
		# and the app needed two, so the purple "Minor's schedule" badge had a
		# renderer and no data. An EMPTY list here is a real answer — this policy
		# has no minor rows, so a minor falls back to the adult table above, and
		# the app can say which of the two it drew.
		"minor_rest_schedule": _schedule_rows(row.get("minor_rest_schedule")),
		"minor_meal_schedule": _schedule_rows(row.get("minor_meal_schedule")),
		"minor_max_hours_without_rest": row.get("minor_max_hours_without_rest") or None,
		"has_minor_schedule": bool(row.get("minor_rest_schedule") or row.get("minor_meal_schedule")),
		"minor_note": (
			"OAR 839-021-0072 — a worker under eighteen is owed a rest period every two hours "
			"and a meal every four. `is_minor` is derived from `date_of_birth` on the crew "
			"roster and is stored nowhere; it is three-valued, and null means no date of birth "
			"is on file rather than that somebody is an adult."
		),
		"notes": row.get("notes") or None,
	}


def _schedule_rows(rows) -> list:
	"""One rest-or-meal table, in the shape the handset already reads.

	The adult tables are spelled out inline above and this is the same shaping —
	extracted rather than copied a third and fourth time, because four hand-typed
	copies of one row shape is how the minor tables come to be missing a key the
	adult ones have.
	"""
	return [
		{
			"hours_from": entry.get("hours_from"),
			"hours_to": entry.get("hours_to"),
			"periods_owed": entry.get("periods_owed"),
			"minutes_each": entry.get("minutes_each"),
			"paid": bool(entry.get("paid")),
		}
		for entry in (rows or [])
	]


# ── 11a. create_break_policy ────────────────────────────────────────────

_SCHEDULE_TABLES = ("rest_schedule", "meal_schedule", "minor_rest_schedule", "minor_meal_schedule")


def _validated_schedule_rows(raw, label: str) -> list[dict]:
	"""Validate and normalise a list of break-schedule row dicts."""
	if not isinstance(raw, list):
		raise ToolError(f"{label} must be a list of row objects, got {type(raw).__name__}.")
	out = []
	for i, entry in enumerate(raw):
		if not isinstance(entry, dict):
			raise ToolError(f"{label}[{i}] must be an object, got {type(entry).__name__}.")
		hours_from = entry.get("hours_from")
		hours_to = entry.get("hours_to")
		periods_owed = entry.get("periods_owed")
		minutes_each = entry.get("minutes_each")
		missing = []
		if hours_from is None:
			missing.append("hours_from")
		if hours_to is None:
			missing.append("hours_to")
		if periods_owed is None:
			missing.append("periods_owed")
		if minutes_each is None:
			missing.append("minutes_each")
		if missing:
			raise ToolError(
				f"{label}[{i}] is missing {', '.join(missing)}. "
				"Each row needs hours_from, hours_to, periods_owed and minutes_each."
			)
		try:
			hours_from = float(hours_from)
			hours_to = float(hours_to)
		except (TypeError, ValueError):
			raise ToolError(f"{label}[{i}]: hours_from and hours_to must be numbers.") from None
		try:
			periods_owed = int(periods_owed)
			minutes_each = int(minutes_each)
		except (TypeError, ValueError):
			raise ToolError(f"{label}[{i}]: periods_owed and minutes_each must be integers.") from None
		if hours_from < 0 or hours_to <= hours_from:
			raise ToolError(f"{label}[{i}]: hours_from must be >= 0 and hours_to must be > hours_from.")
		if periods_owed < 1:
			raise ToolError(f"{label}[{i}]: periods_owed must be >= 1.")
		if minutes_each < 1:
			raise ToolError(f"{label}[{i}]: minutes_each must be >= 1.")
		paid = entry.get("paid", True)
		if isinstance(paid, str):
			paid = paid.strip().lower() in ("1", "true", "yes")
		out.append(
			{
				"hours_from": hours_from,
				"hours_to": hours_to,
				"periods_owed": periods_owed,
				"minutes_each": minutes_each,
				"paid": 1 if paid else 0,
			}
		)
	return out


def _validated_heat_rows(raw, label: str) -> list[dict]:
	"""Validate and normalise a list of heat-break row dicts."""
	if not isinstance(raw, list):
		raise ToolError(f"{label} must be a list of row objects, got {type(raw).__name__}.")
	out = []
	for i, entry in enumerate(raw):
		if not isinstance(entry, dict):
			raise ToolError(f"{label}[{i}] must be an object, got {type(entry).__name__}.")
		hi_from = entry.get("heat_index_from")
		hi_to = entry.get("heat_index_to")
		mins = entry.get("minutes_each")
		every = entry.get("every_hours")
		missing = []
		if hi_from is None:
			missing.append("heat_index_from")
		if hi_to is None:
			missing.append("heat_index_to")
		if mins is None:
			missing.append("minutes_each")
		if every is None:
			missing.append("every_hours")
		if missing:
			raise ToolError(
				f"{label}[{i}] is missing {', '.join(missing)}. "
				"Each row needs heat_index_from, heat_index_to, minutes_each and every_hours."
			)
		concurrent = entry.get("concurrent_with_rest", True)
		if isinstance(concurrent, str):
			concurrent = concurrent.strip().lower() in ("1", "true", "yes")
		out.append(
			{
				"heat_index_from": float(hi_from),
				"heat_index_to": float(hi_to),
				"minutes_each": int(mins),
				"every_hours": float(every),
				"concurrent_with_rest": 1 if concurrent else 0,
			}
		)
	return out


def create_break_policy(args: dict) -> ToolResult:
	"""Create a Labor Break Policy — the break schedule for one state."""
	compat.require_doctype(
		BREAK_POLICY_DOCTYPE,
		"The Labor Break Policy DocType ships with erpnext_mcp v0.58.0 — run `bench migrate`.",
	)
	employee_tool.require_shift_role()

	work_state = as_str(args, "work_state", required=True).upper()
	if work_state not in _VALID_STATES:
		raise ToolError(f"work_state must be one of {', '.join(_VALID_STATES)}, got {work_state!r}.")

	effective_from = as_date(args, "effective_from", required=True)

	policy_id = as_str(args, "policy_id") or f"{work_state}-{effective_from}"
	if frappe.db.exists(BREAK_POLICY_DOCTYPE, policy_id):
		raise ToolError(f"A Labor Break Policy named {policy_id!r} already exists. Nothing was created.")

	enabled = as_bool(args, "enabled", default=True)
	effective_to = as_date(args, "effective_to")
	regulation_citations = as_str(args, "regulation_citations")
	max_hours = args.get("max_hours_without_rest")
	if max_hours is not None:
		max_hours = float(max_hours)
	minor_max = args.get("minor_max_hours_without_rest")
	if minor_max is not None:
		minor_max = float(minor_max)
	notes = as_str(args, "notes")

	doc = frappe.new_doc(BREAK_POLICY_DOCTYPE)
	doc.policy_id = policy_id
	doc.work_state = work_state
	doc.effective_from = effective_from
	doc.enabled = 1 if enabled else 0
	if effective_to:
		doc.effective_to = effective_to
	if regulation_citations:
		doc.regulation_citations = regulation_citations
	if max_hours is not None:
		doc.max_hours_without_rest = max_hours
	if minor_max is not None:
		doc.minor_max_hours_without_rest = minor_max
	if notes:
		doc.notes = notes

	for table_name in _SCHEDULE_TABLES:
		raw = args.get(table_name)
		if raw:
			for row_dict in _validated_schedule_rows(raw, table_name):
				doc.append(table_name, row_dict)

	raw_heat = args.get("heat_schedule")
	if raw_heat:
		for row_dict in _validated_heat_rows(raw_heat, "heat_schedule"):
			doc.append("heat_schedule", row_dict)

	doc.insert(ignore_permissions=True)

	policy_dict = _describe_break_policy(dict(doc.as_dict()))
	return ToolResult(
		data=policy_dict,
		summary=f"created break policy {doc.name} for {work_state} effective {effective_from}",
		docstatus_delta="none → 0 (created)",
	)


# ── 11b. update_break_policy ────────────────────────────────────────────


def update_break_policy(args: dict) -> ToolResult:
	"""Update an existing Labor Break Policy."""
	compat.require_doctype(
		BREAK_POLICY_DOCTYPE,
		"The Labor Break Policy DocType ships with erpnext_mcp v0.58.0 — run `bench migrate`.",
	)
	employee_tool.require_shift_role()

	policy_name = as_str(args, "policy") or as_str(args, "name") or as_str(args, "policy_id")
	if not policy_name:
		raise ToolError("policy is required — the policy_id or docname of the policy to update.")

	if not frappe.db.exists(BREAK_POLICY_DOCTYPE, policy_name):
		raise ToolError(f"No Labor Break Policy named {policy_name!r}. Nothing was changed.")

	if as_str(args, "work_state"):
		raise ToolError(
			"work_state cannot be changed — create a new policy for the other state instead. "
			"Nothing was changed."
		)

	doc = frappe.get_doc(BREAK_POLICY_DOCTYPE, policy_name)
	changes = {}

	enabled_val = as_bool(args, "enabled", default=None)
	if enabled_val is not None:
		before = bool(doc.enabled)
		if enabled_val != before:
			changes["enabled"] = [before, enabled_val]
			doc.enabled = 1 if enabled_val else 0

	new_effective_from = as_date(args, "effective_from")
	if new_effective_from and str(new_effective_from) != str(doc.effective_from or ""):
		changes["effective_from"] = [str(doc.effective_from or ""), new_effective_from]
		doc.effective_from = new_effective_from

	new_effective_to = as_date(args, "effective_to")
	if new_effective_to is not None:
		old = str(doc.effective_to or "")
		if new_effective_to != old:
			changes["effective_to"] = [old or None, new_effective_to or None]
			doc.effective_to = new_effective_to or None

	new_citations = as_str(args, "regulation_citations")
	if new_citations and new_citations != (doc.regulation_citations or ""):
		changes["regulation_citations"] = [doc.regulation_citations or None, new_citations]
		doc.regulation_citations = new_citations

	new_max = args.get("max_hours_without_rest")
	if new_max is not None:
		new_max = float(new_max)
		old_max = doc.max_hours_without_rest or None
		if new_max != old_max:
			changes["max_hours_without_rest"] = [old_max, new_max]
			doc.max_hours_without_rest = new_max

	new_minor_max = args.get("minor_max_hours_without_rest")
	if new_minor_max is not None:
		new_minor_max = float(new_minor_max)
		old_mm = doc.minor_max_hours_without_rest or None
		if new_minor_max != old_mm:
			changes["minor_max_hours_without_rest"] = [old_mm, new_minor_max]
			doc.minor_max_hours_without_rest = new_minor_max

	new_notes = as_str(args, "notes")
	if new_notes and new_notes != (doc.notes or ""):
		changes["notes"] = [doc.notes or None, new_notes]
		doc.notes = new_notes

	for table_name in _SCHEDULE_TABLES:
		raw = args.get(table_name)
		if raw is not None:
			validated = _validated_schedule_rows(raw, table_name)
			old_count = len(doc.get(table_name) or [])
			doc.set(table_name, [])
			for row_dict in validated:
				doc.append(table_name, row_dict)
			changes[table_name] = [f"{old_count} rows", f"{len(validated)} rows"]

	raw_heat = args.get("heat_schedule")
	if raw_heat is not None:
		validated = _validated_heat_rows(raw_heat, "heat_schedule")
		old_count = len(doc.get("heat_schedule") or [])
		doc.set("heat_schedule", [])
		for row_dict in validated:
			doc.append("heat_schedule", row_dict)
		changes["heat_schedule"] = [f"{old_count} rows", f"{len(validated)} rows"]

	if not changes:
		raise ToolError("Nothing to change — every field matches the stored value.")

	doc.save(ignore_permissions=True)

	policy_dict = _describe_break_policy(dict(doc.as_dict()))
	policy_dict["changes"] = changes
	return ToolResult(
		data=policy_dict,
		summary=f"updated break policy {doc.name}: {', '.join(changes)}",
		docstatus_delta="0 → 0 (amended)",
	)


# ── 12. get_shift_production ─────────────────────────────────────────────

BUCKET_LOG = "Bucket Log Entry"


def get_shift_production(args: dict) -> ToolResult:
	"""Per-worker bucket counts for a shift, sorted by count desc."""
	_require()
	actor = employee_tool.require_shift_role()
	row = _resolve_shift(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	production = _compute_shift_production(row)

	return ToolResult(
		data=production,
		summary=f"{row['name']}: {production['total_accepted']} buckets, {len(production['workers'])} workers",
	)


def _compute_shift_production(row: dict) -> dict:
	"""Build the production board for a shift."""
	shift_name = row["name"]
	crew = shifts.crew_of(shift_name)

	workers_by_emp = {}
	for member in crew:
		emp = member.get("employee")
		if not emp:
			continue
		workers_by_emp[emp] = {
			"employee": emp,
			"employee_name": member.get("employee_name") or emp,
			"badge_id": None,
			"joined_at": str(member.get("joined_at") or "") or None,
			"left_at": str(member.get("left_at") or "") or None,
			"buckets_accepted": 0,
			"buckets_rejected": 0,
			"hours_present": shifts.hours_between(
				str(member.get("joined_at") or row.get("start_datetime") or ""),
				str(member.get("left_at") or row.get("end_datetime") or frappe.utils.now()),
			),
		}

	# Read badge mappings
	if compat.doctype_exists("Bucket Log Badge Map"):
		for bm in frappe.db.get_all(
			"Bucket Log Badge Map",
			filters={"employee": ("in", list(workers_by_emp.keys()))},
			fields=["employee", "badge_id"],
		):
			if bm["employee"] in workers_by_emp:
				workers_by_emp[bm["employee"]]["badge_id"] = bm.get("badge_id")

	# Read bucket counts
	total_accepted = 0
	total_rejected = 0
	unattributed = 0
	if compat.doctype_exists(BUCKET_LOG):
		picker_field = compat.first_field(BUCKET_LOG, "picker_id", "employee", "picker", "worker")
		if picker_field:
			status_field = compat.first_field(BUCKET_LOG, "status", "verdict")
			fields = ["name", picker_field]
			if status_field:
				fields.append(status_field)

			shift_filter = compat.first_field(BUCKET_LOG, "shift", "farm_shift")
			if shift_filter:
				entries = frappe.db.get_all(
					BUCKET_LOG,
					filters={shift_filter: shift_name},
					fields=fields,
					limit_page_length=0,
				)
				for entry in entries:
					picker = entry.get(picker_field)
					status = str(entry.get(status_field) or "Accepted") if status_field else "Accepted"
					if picker and picker in workers_by_emp:
						if status in ("Accepted", "Linked"):
							workers_by_emp[picker]["buckets_accepted"] += 1
							total_accepted += 1
						else:
							workers_by_emp[picker]["buckets_rejected"] += 1
							total_rejected += 1
					elif picker:
						total_accepted += 1
						unattributed += 1
					else:
						total_accepted += 1
						unattributed += 1

	# Break reconciliation
	events = shifts.events_of(shift_name)
	policy_name = row.get("break_policy")
	policy_dict = {}
	if policy_name and compat.doctype_exists(BREAK_POLICY_DOCTYPE):
		try:
			pdoc = frappe.get_doc(BREAK_POLICY_DOCTYPE, policy_name)
			policy_dict = _describe_break_policy(dict(pdoc.as_dict()))
		except Exception:
			pass

	break_events = [dict(ev) for ev in events if ev.get("break_kind")]
	for emp, w in workers_by_emp.items():
		seg = {
			"employee": emp,
			"joined_at": w["joined_at"] or str(row.get("start_datetime") or ""),
			"left_at": w["left_at"] or str(row.get("end_datetime") or ""),
		}
		if policy_dict:
			wb = breaks_mod.worker_breaks(seg, break_events, policy_dict)
			w["rest_periods_owed"] = wb["rest_owed"]
			w["rest_periods_taken"] = wb["rest_taken"]
			w["meal_periods_owed"] = wb["meal_owed"]
			w["meal_periods_taken"] = wb["meal_taken"]
			w["paid_break_minutes"] = round(wb["paid_break_hours"] * 60, 0)
			w["unpaid_break_minutes"] = round(wb["unpaid_break_hours"] * 60, 0)

	workers = sorted(workers_by_emp.values(), key=lambda w: w["buckets_accepted"], reverse=True)
	still_on = len([m for m in crew if not m.get("left_at")])

	return {
		"shift": shift_name,
		"as_of": frappe.utils.now(),
		"crew_size": len(crew),
		"still_on_shift": still_on,
		"total_accepted": total_accepted,
		"total_rejected": total_rejected,
		"workers": workers,
		"unattributed_entries": unattributed,
	}


# ── 13. get_shift_crew_timeline ──────────────────────────────────────────

#: The event types that are evidence somebody was LOOKED AFTER rather than
#: merely present. Counted per worker inside their own envelope, because
#: "the crew took three water breaks" is not an answer about the person who
#: arrived after two of them.
CARE_EVENTS = (
	"Water Break",
	"Shade Break",
	"Rest Cycle",
	"Cool-Down",
	"Supervisor Observation",
	"Heat Illness Signs Check",
	"Rest Period",
	"Meal Period",
)


def get_shift_crew_timeline(args: dict) -> ToolResult:
	"""Every crew member's own envelope: their span, their weather, their events.

	v0.64.0. THE SHIFT IS ONE RECORD AND THE CREW IS NOT ONE PERSON, and this is
	the tool that stops the first fact from erasing the second. `get_shift`
	answers "what happened on this shift"; `get_weather_timeline` answers "how hot
	did it get". Neither answers the question a wage claim and a heat citation
	both turn on, which is what happened TO ANA — who joined at 09:40, left at
	13:00, and was therefore present for two of the shift's five water breaks and
	absent for the hour it was hottest.

	EVERY NUMBER HERE IS COMPUTED AGAINST THE WORKER'S OWN SPAN, never the
	shift's. That is the entire point:

	  * `readings_in_span` and `peak` are the conditions THEY stood in. The
	    foreman's 96 °F at three in the afternoon is not evidence about a picker
	    who went home at one.
	  * `first_crossing_in_span` is when OAR 437-004-1131's obligations started
	    running FOR THEM. A worker who arrived after the crossing has a later
	    clock than the shift's, and `present_at_shift_first_crossing` says which.
	  * `care_events_in_span` counts only the water, shade, rest and observation
	    events that fell inside their envelope, plus the Individual-scoped ones
	    that name them. A crew-scoped break at 08:00 is not care given to somebody
	    who arrived at 09:40, and counting it would be the record flattering the
	    operation in exactly the place an investigator checks.

	NOTHING IS INTERPOLATED, and `minutes_bracketed_by_crossings` is named the
	way it is for that reason. It is the elapsed time from the first at-or-above
	reading in the worker's span to the last one — a BRACKET, not a sum of
	exposure — because the readings are samples every fifteen minutes and the
	temperature between two of them is a thing nobody measured. `sample_gap_
	minutes` reports the actual cadence so a reader can see how coarse the
	bracket is; a timeline reconstructed hourly from the archive brackets the
	same afternoon far more loosely than a live one, and the two must not read
	alike.

	IT IS READ-ONLY AND IT WRITES NOTHING BACK. `present_until` is computed from
	an empty `left_at` for the same reason `describe_crew_row` computes it:
	writing it would destroy the difference between "left at 13:00" and "stayed
	to the end" the moment the shift's end time changed.
	"""
	_require()
	actor = employee_tool.require_shift_role()
	row = _resolve_shift(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	from ..services import weather as weather_service

	shift_name = row["name"]
	shift_end = str(row.get("end_datetime") or "") or None
	shift_start = str(row.get("start_datetime") or "") or None
	crew = shifts.crew_of(shift_name)
	events = shifts.events_of(shift_name)
	readings = shifts.weather_of(shift_name)
	limits = weather_service.thresholds_for(str(row.get("company") or ""))

	only = as_str(args, "employee")
	if only:
		crew = [entry for entry in crew if str(entry.get("employee") or "") == only]
		if not crew:
			raise ToolError(
				f"{only} is not on {shift_name}'s crew. A person who was not rostered has no "
				"envelope on this shift — get_shift has the crew list. Nothing was changed."
			)

	# The shift's OWN first crossing, computed once. Every worker is then asked
	# whether they were standing there when it happened, which is a different
	# question from whether it happened at all.
	shift_crossings = [
		str(entry.get("reading_datetime") or "")
		for entry in readings
		if weather_service._heat_crossing(entry, limits)
	]
	shift_first_crossing = shift_crossings[0] if shift_crossings else None

	policy = _break_policy_dict(row)
	break_events = [dict(entry) for entry in events if entry.get("break_kind")]

	workers = [
		_crew_envelope(
			member,
			shift_row=row,
			shift_end=shift_end,
			readings=readings,
			events=events,
			break_events=break_events,
			policy=policy,
			limits=limits,
			shift_first_crossing=shift_first_crossing,
		)
		for member in crew
	]

	exposed = [entry["employee"] for entry in workers if entry["exposure"]["first_crossing_in_span"]]
	arrived_after = [
		entry["employee"]
		for entry in workers
		if shift_first_crossing and not entry["exposure"]["present_at_shift_first_crossing"]
	]
	short = [entry["employee"] for entry in workers if entry["breaks"] and entry["breaks"].get("short")]

	data = {
		"shift": shift_name,
		"company": row.get("company"),
		"shift_type": row.get("shift_type") or None,
		"location": row.get("location") or None,
		"start_datetime": shift_start,
		"end_datetime": shift_end,
		"open": shifts.is_open(row),
		"foreman": row.get("foreman"),
		"foreman_name": row.get("foreman_name"),
		"crew_size": len(crew),
		"still_on_shift": len([entry for entry in crew if not entry.get("left_at")]),
		"thresholds": limits,
		"shift_first_crossing": shift_first_crossing,
		"weather_reading_count": len(readings),
		"sample_gap_minutes": _sample_gap_minutes(readings),
		"crew": workers,
		"exposed_to_the_heat_threshold": exposed,
		"arrived_after_the_first_crossing": arrived_after,
		"short_of_their_break_entitlement": short,
		"break_policy": row.get("break_policy") or None,
	}

	if not readings:
		data["weather_note"] = (
			"THIS SHIFT HAS NO WEATHER TIMELINE, so every exposure figure below is null rather "
			"than zero — nobody measured, which is not the same as nothing happened. An open "
			"shift with farm_location_gps collects readings on the quarter-hour sweep; a closed "
			"one is reconstructed by backfill_weather_for_shift; a shift with no coordinates "
			"never collects anything and never will."
		)
	elif shift_first_crossing and arrived_after:
		data["exposure_note"] = (
			f"{len(arrived_after)} of {len(workers)} crew member(s) were not on this shift when it "
			f"first crossed the heat threshold at {shift_first_crossing}. {shifts.CITATION}'s "
			"obligations run from the crossing FOR THE PEOPLE WHO WERE EXPOSED TO IT, so their "
			"clock is their own joined_at and not the shift's — and a heat record that documents "
			"one exposure period for a crew that turned over across the afternoon is documenting "
			"a day that did not happen to most of them."
		)
	if short:
		data["break_note"] = (
			f"{len(short)} crew member(s) took fewer rest or meal periods than their own hours on "
			"this shift entitle them to. Computed per person against their own span, which is the "
			"only way it can be right: entitlement is a function of hours worked, and a worker who "
			"put in four hours is owed a different number from the foreman who put in ten."
		)
	if not crew:
		data["crew_note"] = (
			"NO CREW ROWS. A shift with no crew is a record that nobody was at work, which is "
			"almost always a shift formed but never rostered — add_worker_to_shift is what fills "
			"it, and remove_worker_from_shift sets left_at rather than deleting the row, so an "
			"empty crew here is never somebody who has been taken off."
		)

	return ToolResult(
		data=data,
		summary=(
			f"{shift_name}: {len(crew)} crew envelope(s)"
			+ (f", {len(exposed)} exposed at or above threshold" if readings else "")
			+ (f", {len(short)} short of break entitlement" if short else "")
		),
	)


def _break_policy_dict(row: dict) -> dict:
	"""The shift's break policy as a plain dict, or `{}` where there is none."""
	policy_name = row.get("break_policy")
	if not policy_name or not compat.doctype_exists(BREAK_POLICY_DOCTYPE):
		return {}
	try:
		return _describe_break_policy(dict(frappe.get_doc(BREAK_POLICY_DOCTYPE, policy_name).as_dict()))
	except Exception:
		# A policy that has been deleted out from under a shift is a gap in the
		# entitlement figures and not a reason to refuse the whole read — the
		# spans, the weather and the events are all still true without it.
		return {}


def _sample_gap_minutes(readings: list):
	"""The MEDIAN gap between readings, which is how coarse the bracket below is.

	Median rather than mean because a shift that was backfilled for its morning
	and fetched live for its afternoon has one enormous gap at the join, and a
	mean would report a cadence that describes neither half.
	"""
	stamps = [str(entry.get("reading_datetime") or "") for entry in readings]
	stamps = sorted(stamp for stamp in stamps if stamp)
	if len(stamps) < 2:
		return None
	gaps = []
	for earlier, later in itertools.pairwise(stamps):
		try:
			gaps.append(float(frappe.utils.time_diff_in_seconds(later, earlier)) / 60.0)
		except Exception:
			continue
	if not gaps:
		return None
	gaps.sort()
	middle = len(gaps) // 2
	value = gaps[middle] if len(gaps) % 2 else (gaps[middle - 1] + gaps[middle]) / 2.0
	return round(value, 1)


def _within(stamp: str, start: str, end: str) -> bool:
	"""Is `stamp` inside `[start, end]`? An unknown bound does not exclude.

	A missing `joined_at` means nobody wrote down when they arrived, and treating
	that as "arrived at the end of time" would empty their envelope and report a
	worker who was looked after all day as one who was never there at all.
	"""
	if not stamp:
		return False
	if start and stamp < start:
		return False
	if end and stamp > end:
		return False
	return True


def _crew_envelope(
	member: dict,
	shift_row: dict,
	shift_end: str,
	readings: list,
	events: list,
	break_events: list,
	policy: dict,
	limits: dict,
	shift_first_crossing,
) -> dict:
	"""One crew member's own span, and everything true inside it."""
	from ..services import weather as weather_service

	employee = str(member.get("employee") or "")
	described = shifts.describe_crew_row(member, shift_end or "")
	joined = described["joined_at"] or str(shift_row.get("start_datetime") or "")
	until = described["present_until"] or str(shift_row.get("end_datetime") or "") or frappe.utils.now()

	mine = [entry for entry in readings if _within(str(entry.get("reading_datetime") or ""), joined, until)]
	crossings = [
		str(entry.get("reading_datetime") or "")
		for entry in mine
		if weather_service._heat_crossing(entry, limits)
	]

	def peak(fieldname):
		values = []
		for entry in mine:
			value = entry.get(fieldname)
			if value in (None, ""):
				continue
			try:
				values.append(float(value))
			except (TypeError, ValueError):
				continue
		return max(values) if values else None

	bracket = None
	if len(crossings) >= 2:
		bracket = shifts.hours_between(crossings[0], crossings[-1])
		bracket = round(bracket * 60.0, 1) if bracket is not None else None
	elif len(crossings) == 1:
		# ONE READING IS A MOMENT AND NOT A DURATION. Zero is the honest answer:
		# the crew was above the threshold when somebody measured, and how long
		# either side of that is a thing nobody recorded.
		bracket = 0.0

	mine_events = []
	for entry in events:
		when = str(entry.get("event_datetime") or "")
		scope = str(entry.get("applies_to") or "Crew")
		named = str(entry.get("employee") or "")
		if scope == "Individual":
			if named != employee:
				continue
		elif not _within(when, joined, until):
			continue
		mine_events.append(shifts.describe_event_row(entry))

	care = [entry for entry in mine_events if entry["event_type"] in CARE_EVENTS]

	out = {
		**described,
		"employee": employee,
		"hours_present": shifts.hours_between(joined, until),
		"span": {"from": joined or None, "to": until or None},
		"pay_type": member.get("pay_type") or shift_row.get("pay_type") or None,
		"pay_rate": member.get("pay_rate")
		if member.get("pay_rate") not in (None, "")
		else shift_row.get("pay_rate"),
		"pay_basis_from": "crew row"
		if member.get("pay_type")
		else ("shift" if shift_row.get("pay_type") else None),
		"exposure": {
			"readings_in_span": len(mine),
			"peak_temp_f": peak("temp_f"),
			"peak_heat_index_f": peak("heat_index_f"),
			"peak_wind_speed_mph": peak("wind_speed_mph"),
			"readings_at_or_above_the_heat_threshold": len(crossings),
			"first_crossing_in_span": crossings[0] if crossings else None,
			"last_crossing_in_span": crossings[-1] if crossings else None,
			"minutes_bracketed_by_crossings": bracket,
			"present_at_shift_first_crossing": (
				_within(shift_first_crossing, joined, until) if shift_first_crossing else None
			),
		},
		"events_in_span": len(mine_events),
		"care_events_in_span": len(care),
		"events": mine_events,
	}

	if policy:
		wb = breaks_mod.worker_breaks(
			{"employee": employee, "joined_at": joined, "left_at": until},
			break_events,
			policy,
		)
		out["breaks"] = {
			"rest_owed": wb["rest_owed"],
			"rest_taken": wb["rest_taken"],
			"meal_owed": wb["meal_owed"],
			"meal_taken": wb["meal_taken"],
			"paid_break_minutes": round(wb["paid_break_hours"] * 60.0, 1),
			"unpaid_break_minutes": round(wb["unpaid_break_hours"] * 60.0, 1),
			"short": bool(wb["rest_taken"] < wb["rest_owed"] or wb["meal_taken"] < wb["meal_owed"]),
		}
	else:
		out["breaks"] = None
	return out

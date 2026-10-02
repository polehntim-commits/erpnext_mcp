# SPDX-License-Identifier: MIT
"""Crew task tools: put people on a task, take them off, and say who is on it. v0.213.0.

docs/design/crew_tasks.md. The model, the roll-up and the closes are
`erpnext_mcp/crew_tasks.py`; this file is the five gated tools.

ONE GATE FOR BOTH TRANSPORTS. Adding and removing crew is for a Foreman, a Farm
Manager or a Crew Leader, and the check is here — in the tool — so the MCP path
and the phone path cannot disagree about who may. A crew member who is none of
those may read their own crew and update their own row, and nothing else.
"""

from __future__ import annotations

import json

import frappe

from .. import compat, crew_tasks, qualifications, security
from .. import shifts as shift_records
from ..args import as_bool, as_str
from ..errors import ToolError
from ..result import ToolResult
from . import dispatch
from . import employee as employee_tool

FARM_TASK = dispatch.FARM_TASK
ASSIGNMENT = dispatch.FARM_TASK_ASSIGNMENT
EMPLOYEE = "Employee"

ADD_CAP = 150


# ── who is asking ───────────────────────────────────────────────────────────
def _actor() -> str:
	return security.caller_identity() or str(getattr(frappe.session, "user", "") or "")


def is_supervisor(actor: str = "") -> bool:
	actor = actor or _actor()
	if not actor or actor == "Guest":
		return False
	from .. import roles

	held = set(frappe.get_roles(actor) or []) or set(roles.all_roles_of(actor) or [])
	return bool(held & set(crew_tasks.ROLES))


def require_supervisor(what: str) -> str:
	return employee_tool._require_one_of(crew_tasks.ROLES, "a crew change", what)


def _caller_employee(actor: str = "") -> str:
	return dispatch._employee_for_user(actor or _actor())


# ── arguments ───────────────────────────────────────────────────────────────
def _list(args: dict, key: str) -> list:
	raw = args.get(key)
	if isinstance(raw, str):
		text = raw.strip()
		if text.startswith("["):
			try:
				raw = json.loads(text)
			except Exception as error:
				raise ToolError(f"{key} is not a valid JSON list ({error}). Nothing was changed.") from error
		else:
			raw = [part for part in text.split(",")] if text else []
	if raw in (None, ""):
		return []
	if not isinstance(raw, (list, tuple)):
		raise ToolError(f"{key} is a list. Nothing was changed.")
	out = []
	for entry in raw:
		value = str(entry or "").strip()
		if value and value not in out:
			out.append(value)
	return out


def _crew_task(args: dict, verb: str, open_only: bool = True) -> dict:
	dispatch._require()
	if not crew_tasks.ready():
		raise ToolError(
			"this site has not migrated the crew task fields (v0.213.0) — run "
			f"`bench --site <site> migrate`. Nothing was {verb}."
		)
	row = dispatch.task_row(as_str(args, "task", required=True))
	if not crew_tasks.is_crew(row):
		raise ToolError(
			f"{row['name']} is an individual task: one person holds it, and assign_farm_task sends "
			"them. A task is a crew task when it is raised with work_mode 'Crew' (create_farm_task, "
			f"or its template). Nothing was {verb}."
		)
	if open_only:
		if row["state"] in dispatch.TERMINAL_STATES or row["state"] == dispatch.AWAITING_REVIEW:
			raise ToolError(
				f"{row['name']} is {row['state']} — the work is finished, and its crew's time ended "
				f"with it. Raise a fresh task. Nothing was {verb}."
			)
		if row["state"] == dispatch.DRAFT:
			raise ToolError(f"{row['name']} is still a Draft. Publish it first. Nothing was {verb}.")
	return row


def _people(args: dict, company: str, results: list) -> list:
	"""[(employee, badge)] from `employees`, `badge_ids` and `shift`. Bad badges become lines."""
	people: list = [(name, "") for name in _list(args, "employees")]
	single = as_str(args, "employee") or as_str(args, "assigned_to")
	if single:
		people.append((single, ""))
	for badge in _list(args, "badge_ids"):
		try:
			from . import badges

			card = badges.resolve_badge({"badge_id": badge, "company": company}).data
			people.append((str(card.get("employee") or ""), badge))
		except ToolError as error:
			results.append({"badge_id": badge, "employee": None, "outcome": "refused", "reason": str(error)})
	shift = as_str(args, "shift") or as_str(args, "farm_shift")
	if shift:
		shift = dispatch._shift_argument({"farm_shift": shift}, company)
		for member in shift_records.crew_of(shift):
			if member.get("employee") and not member.get("left_at"):
				people.append((str(member["employee"]), ""))
	seen, unique = set(), []
	for person, badge in people:
		if person and person not in seen:
			seen.add(person)
			unique.append((person, badge))
	if len(unique) > ADD_CAP:
		raise ToolError(f"at most {ADD_CAP} people in one call. Nothing was changed.")
	return unique


def _line(person: str, badge: str = "") -> dict:
	line = {
		"employee": person,
		"employee_name": (
			frappe.db.get_value(EMPLOYEE, person, "employee_name")
			if compat.doctype_exists(EMPLOYEE)
			else None
		)
		or person,
	}
	if badge:
		line["badge_id"] = badge
	return line


def _counts(results: list, names: tuple) -> dict:
	return {name: sum(1 for line in results if line.get("outcome") == name) for name in names}


# ── the lead ────────────────────────────────────────────────────────────────
def _ensure_lead(row: dict, args: dict, actor: str) -> tuple[dict, bool]:
	"""Make sure the task has a lead who has started it. Returns (task row, started now)."""
	asked = as_str(args, "lead")
	if asked:
		asked = dispatch._worker({"assigned_to": asked}, "lead")
	holder = dispatch.live_assignment(row["name"])
	current = str(row.get("assigned_to") or "") if holder else ""
	wanted = asked or current or _caller_employee(actor)
	if not wanted:
		raise ToolError(
			f"{row['name']} has no lead yet, and this call is not from somebody with an Employee "
			"record. Pass lead — the supervisor who starts the task and closes it with its "
			"evidence. Nothing was changed."
		)
	if wanted != current:
		inner = {"task": row["name"], "assigned_to": wanted}
		if holder:
			inner.update(reassign=True, reason="Crew lead changed")
		for key in ("override_phi", "phi_override_reason"):
			if args.get(key) is not None:
				inner[key] = args[key]
		dispatch.assign_holder(inner)
	started = False
	holder = dispatch.live_assignment(row["name"])
	if holder and str(frappe.db.get_value(ASSIGNMENT, holder, "state")) == dispatch.CLAIMED:
		dispatch.start_farm_task({"assignment": holder})
		started = True
	return dispatch.task_row(row["name"]), started


# ── 1. add_to_crew_task ─────────────────────────────────────────────────────
def add_to_crew_task(args: dict) -> ToolResult:
	"""Put people on a crew task: a whole shift, named people, scanned badges, or a mix."""
	actor = require_supervisor("add people to a crew task")
	row = _crew_task(args, "changed")
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))
	dispatch.lock_task(row["name"])
	row = _crew_task(args, "changed")
	company = str(row.get("company") or "")

	results: list = []
	people = _people(args, company, results)
	if not people and not results and not as_str(args, "lead"):
		raise ToolError(
			"who is going on the crew? Pass employees, badge_ids or shift (or lead, to set who "
			"leads it). Nothing was changed."
		)

	row, started = _ensure_lead(row, args, actor)
	section = as_str(args, "section")
	if section and section not in {s["key"] for s in crew_tasks.sections_of(row.get("crew_sections"))}:
		raise ToolError(
			f"{row['name']} has no section {section!r}. update_crew_task_sections sets them. "
			"Nothing was changed."
		)
	request = as_str(args, "client_request_id")
	now = str(frappe.utils.now())
	added: list = []

	for person, badge in people:
		line = _line(person, badge)
		try:
			if compat.doctype_exists(EMPLOYEE):
				if not frappe.db.exists(EMPLOYEE, person):
					raise ToolError(f"no Employee {person!r} on this site.")
				theirs = str(frappe.db.get_value(EMPLOYEE, person, "company") or "")
				if company and theirs and theirs != company:
					raise ToolError(f"{line['employee_name']} is at {theirs}, and this task is at {company}.")
			if crew_tasks.on_crew(row["name"], person):
				line["outcome"] = "already"
				results.append(line)
				continue
			dispatch._refuse_a_minor_on_prohibited_work(person, line["employee_name"], row, verb="changed")
			qualifications.refuse_unqualified(row, person, "changed")

			# ONE CREW TASK AT A TIME. Their time on the other one ends here, so
			# nobody's minutes are on two blocks at once.
			for other in crew_tasks.open_rows(employee=person):
				crew_tasks.close_row(str(other["name"]), now, f"Moved to {row['name']}", actor)
				crew_tasks.stamp_rollup(str(other["task"]))
				line["moved_from"] = other["task"]

			shift = dispatch._open_shift_for(person, company)
			doc = frappe.new_doc(ASSIGNMENT)
			doc.task = row["name"]
			doc.task_name = row.get("task_name")
			doc.company = company or None
			doc.assigned_to = person
			doc.assigned_to_name = line["employee_name"]
			doc.state = crew_tasks.ON_CREW
			doc.dispatched_by_foreman = 1
			doc.claimed_at = now
			doc.started_at = now
			doc.farm_shift = shift or None
			doc.piece_unit = row.get("crew_piece_unit") or None
			doc.section = section or None
			doc.added_by = actor
			doc.client_request_id = request or None
			doc.insert(ignore_permissions=True)
			line.update(outcome="added", assignment=doc.name, farm_shift=shift or None)
			if not shift:
				line["warning"] = (
					f"{line['employee_name']} is not clocked into a shift, so this time is on the "
					"task and on no shift's record. clock_in_crew puts them on one."
				)
			added.append(person)
		except ToolError as error:
			line.update(outcome="refused", reason=str(error))
		except frappe.ValidationError as error:
			line.update(outcome="refused", reason=str(error))
		results.append(line)

	crew = crew_tasks.stamp_rollup(row["name"])
	data = {
		"task": row["name"],
		"task_name": row.get("task_name"),
		"lead": row.get("assigned_to") or None,
		"lead_name": row.get("assigned_to_name") or None,
		"started": started,
		"results": results,
		**_counts(results, ("added", "already", "refused")),
		"crew": crew_tasks.describe(dispatch.task_row(row["name"])),
		"actor": actor,
	}
	warnings = dispatch._rei_warnings(row)
	if warnings:
		data["warnings"] = warnings
	if added:
		data["push"] = _push(row, added)
	return ToolResult(
		data=data,
		summary=(
			f"{data['added']} added to the crew of {row['name']}"
			+ (f", {data['already']} already on" if data["already"] else "")
			+ (f", {data['refused']} refused" if data["refused"] else "")
			+ (f" — {crew['on_now']} on it now" if crew else "")
		),
	)


def _push(task: dict, people: list) -> dict:
	"""Tell the people just put on the crew. Never raises — see `dispatch._push_assignment`."""
	try:
		return dispatch.push_service.send_push_to_employees(
			people,
			dispatch.push_service.task_payload(
				task=str(task.get("name") or ""),
				task_name=str(task.get("task_name") or ""),
				location=str(task.get("location") or ""),
				urgency=str(task.get("urgency") or ""),
			),
			collapse_id=str(task.get("name") or ""),
		)
	except Exception as error:  # pragma: no cover
		return {
			"employees": 0,
			"tokens": 0,
			"sent": 0,
			"failed": 0,
			"skipped": 0,
			"reason": f"error: {error}",
		}


# ── 2. remove_from_crew_task ────────────────────────────────────────────────
def remove_from_crew_task(args: dict) -> ToolResult:
	"""Take people off a crew task. Their row is closed with a time and a reason, and kept."""
	actor = require_supervisor("take people off a crew task")
	row = _crew_task(args, "changed", open_only=False)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))
	company = str(row.get("company") or "")

	results: list = []
	if as_bool(args, "all", False):
		people = [(str(entry["assigned_to"]), "") for entry in crew_tasks.open_rows(task=row["name"])]
	else:
		people = _people(
			{key: args.get(key) for key in ("employees", "employee", "badge_ids")}, company, results
		)
	if not people and not results:
		raise ToolError("who is coming off? Pass employees, badge_ids, or all=true. Nothing was changed.")

	when = as_str(args, "ended_at")
	reason = as_str(args, "reason") or "Removed from the crew"
	for person, badge in people:
		line = _line(person, badge)
		open_row = crew_tasks.on_crew(row["name"], person)
		if not open_row:
			line["outcome"] = "already"
		else:
			closed = crew_tasks.close_row(str(open_row["name"]), when, reason, actor)
			line.update(
				outcome="removed",
				assignment=closed.get("name"),
				ended_at=str(closed.get("completed_at") or "") or None,
				minutes=int(closed.get("actual_duration_minutes") or 0),
			)
		results.append(line)

	crew_tasks.stamp_rollup(row["name"])
	counts = _counts(results, ("removed", "already", "refused"))
	crew = crew_tasks.describe(dispatch.task_row(row["name"]))
	return ToolResult(
		data={
			"task": row["name"],
			"task_name": row.get("task_name"),
			"results": results,
			**counts,
			"crew": crew,
			"actor": actor,
			"note": "Nobody's row was deleted: each is closed with its end time and reason, and stays.",
		},
		summary=f"{counts['removed']} taken off the crew of {row['name']} — {crew['on_now']} on it now",
	)


# ── 3. update_crew_task_member ──────────────────────────────────────────────
def update_crew_task_member(args: dict) -> ToolResult:
	"""Pieces, notes, section, or "my part is done", on one person's crew row."""
	actor = _actor()
	row = _crew_task(args, "changed", open_only=False)
	mine = _caller_employee(actor)
	person = as_str(args, "employee") or mine
	if not person:
		raise ToolError("employee is required — whose row on this crew. Nothing was changed.")
	supervisor = is_supervisor(actor)
	if supervisor:
		employee_tool.require_company_scope(actor, str(row.get("company") or ""))
	elif person != mine:
		require_supervisor("change somebody else's row on a crew task")

	rows = [r for r in crew_tasks.member_rows(row["name"]) if str(r.get("assigned_to")) == person]
	if not rows:
		raise ToolError(
			f"{person} has never been on the crew of {row['name']}. add_to_crew_task puts them on. "
			"Nothing was changed."
		)
	# The row they are on now; else their latest.
	target = next((r for r in rows if r["state"] == crew_tasks.ON_CREW), rows[-1])
	doc = frappe.get_doc(ASSIGNMENT, target["name"])

	def number(key):
		if args.get(key) in (None, ""):
			return None
		try:
			value = float(args[key])
		except (TypeError, ValueError) as error:
			raise ToolError(f"{key} is a number. Nothing was changed.") from error
		if value < 0:
			raise ToolError(f"{key} cannot be negative. Nothing was changed.")
		return value

	pieces, extra = number("pieces"), number("add_pieces")
	if pieces is not None:
		doc.pieces = pieces
	if extra is not None:
		doc.pieces = float(doc.pieces or 0) + extra
	if as_str(args, "piece_unit"):
		doc.piece_unit = as_str(args, "piece_unit")
	if "notes" in args and args.get("notes") is not None:
		doc.member_notes = as_str(args, "notes") or None
	if as_str(args, "section"):
		if not supervisor:
			require_supervisor("move somebody to another section")
		keys = {s["key"] for s in crew_tasks.sections_of(row.get("crew_sections"))}
		if as_str(args, "section") not in keys:
			raise ToolError(f"{row['name']} has no section {as_str(args, 'section')!r}. Nothing was changed.")
		doc.section = as_str(args, "section")
	doc.save(ignore_permissions=True)

	if args.get("part_done") is not None and as_bool(args, "part_done", False):
		if str(doc.state) == crew_tasks.ON_CREW:
			crew_tasks.close_row(doc.name, "", "Part done", actor, part_done=True)
		else:
			frappe.db.set_value(ASSIGNMENT, doc.name, "part_done", 1)

	crew_tasks.stamp_rollup(row["name"])
	fresh = dict(frappe.get_doc(ASSIGNMENT, doc.name).as_dict())
	return ToolResult(
		data={
			"task": row["name"],
			"member": crew_tasks.describe_member(fresh, default_unit=str(row.get("crew_piece_unit") or "")),
			"crew": crew_tasks.describe(dispatch.task_row(row["name"])),
		},
		summary=f"updated {fresh.get('assigned_to_name') or person} on the crew of {row['name']}",
	)


# ── 4. update_crew_task_sections ────────────────────────────────────────────
def update_crew_task_sections(args: dict) -> ToolResult:
	"""Set a crew task's sections (row ranges), or tick one done."""
	actor = require_supervisor("change a crew task's sections")
	row = _crew_task(args, "changed", open_only=False)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))
	current = crew_tasks.sections_of(row.get("crew_sections"))

	if args.get("sections") is not None:
		current = crew_tasks.normalise_sections(args.get("sections"), current)
	key = as_str(args, "section")
	if key:
		target = next((s for s in current if s["key"] == key), None)
		if target is None:
			raise ToolError(
				f"{row['name']} has no section {key!r} — it has: "
				f"{', '.join(s['key'] for s in current) or 'none'}. Nothing was changed."
			)
		done = as_bool(args, "done", True)
		target["done"] = bool(done)
		target["done_at"] = str(frappe.utils.now()) if done else None
		target["done_by"] = actor if done else None
	elif args.get("sections") is None:
		raise ToolError("pass sections (to set them) or section and done (to tick one). Nothing was changed.")

	frappe.db.set_value(FARM_TASK, row["name"], "crew_sections", json.dumps(current))
	progress = crew_tasks.progress(current)
	return ToolResult(
		data={"task": row["name"], "sections": current, "progress": progress},
		summary=f"{row['name']}: " + (progress["summary"] if progress else "no sections"),
	)


# ── 5. list_crew_task_members ───────────────────────────────────────────────
def list_crew_task_members(args: dict) -> ToolResult:
	"""Who is on a crew task now, who was, and what they did."""
	actor = _actor()
	row = _crew_task(args, "read", open_only=False)
	if not is_supervisor(actor):
		mine = _caller_employee(actor)
		been_on = mine and any(str(r.get("assigned_to")) == mine for r in crew_tasks.member_rows(row["name"]))
		if not been_on and str(row.get("assigned_to") or "") != (mine or "\0"):
			require_supervisor("read a crew you are not on")
	crew = crew_tasks.describe(row)
	return ToolResult(
		data={"task": row["name"], "task_name": row.get("task_name"), "state": row.get("state"), **crew},
		summary=(
			f"{row['name']}: {crew['on_now']} on the crew now, {crew['headcount']} in all, "
			f"{crew['person_minutes']} person-minutes"
		),
	)


# ── the phone's board (not an MCP tool) ─────────────────────────────────────
def open_crew_tasks(companies: list, limit: int = 200) -> list:
	"""Open crew tasks in these entities, described, with their crew counts."""
	if not crew_tasks.ready():
		return []
	filters: dict = {
		"is_crew_task": 1,
		"state": ("in", ["Available", *crew_tasks.WORKING_STATES]),
	}
	if companies:
		filters["company"] = ("in", list(companies))
	names = frappe.db.get_all(FARM_TASK, filters=filters, pluck="name", order_by="creation desc", limit=limit)
	return [dispatch._describe_task(dispatch.task_row(str(name))) for name in names or []]

# SPDX-License-Identifier: MIT
"""Controller for Farm Task — one piece of work, and the evidence closing it needs.

THE NAME IS `FT-YYYY-MM-<seq>`, SEQUENCED WITHIN THE MONTH, AND NOT THE TASK NAME.
The Sprint 8 specification asked for `task_name` as the docname and it is the one
place this implementation departs from it, deliberately and with a reason that
shows up in the second season rather than the first:

  * a habitability walk on MC-Cabin-01 happens EVERY year. A docname built from
    "Habitability walk — MC-Cabin-01" collides with its own history the moment
    the second one is raised, and the failure lands in front of whoever ran
    `generate_tasks_from_compliance_alerts` on a Tuesday in August;
  * fifty-four tasks generated from fifty-four alerts in one call have to produce
    fifty-four distinct names with no human in the loop to disambiguate them.

So `task_name` stays as the title — it is what a foreman reads on the board — and
the key is a sequence. The month is in it for the same reason Housing Assignment
carries one: farm work arrives in the same fortnight every year, and a name
carrying the month sorts into seasons without a report. The sequence is computed
from the rows already in that month rather than from Frappe's naming series,
because the series counter is global and would leave gaps that read like deleted
records in an audit somebody is defending.

`evidence_required` IS MANDATORY AND THAT IS THE WHOLE DESIGN. A task that does
not say what closing it requires is a task that gets closed with a tick in a box.
The controller refuses a blank one, refuses one that is not a JSON object, and
refuses a key it does not recognise — because `{"photo": true}` where `"photos"`
was meant asks for nothing, refuses nothing, and looks exactly like a task with
a photo requirement right up until the audit.

`assigned_to` IS A DATA FIELD, NOT A LINK, for the same reason
`Housing Assignment.employee` is. Frappe HR is not a dependency of this app; a
Link to Employee would make the whole doctype fail to migrate on every site that
has not installed it. The id is validated against the Employee register *where
there is one*.

WHAT THIS REFUSES IS SMALL. A missing evidence contract, a nonsense one, a
negative duration, and a state that is not one of the eight. It does NOT police
the transitions between states: the Kanban board is a foreman dragging a card,
and a controller that refused a drag would make the board a decoration. The
ORDER is enforced in the tools, where the transition carries a claim about who
did what and when — see `erpnext_mcp/tools/dispatch.py`.
"""

import json

import frappe
from frappe import _
from frappe.model.document import Document

#: The ten states, in the order they read across a dispatch board.
DRAFT = "Draft"
AVAILABLE = "Available"
CLAIMED = "Claimed"
IN_PROGRESS = "In-Progress"
PAUSED = "Paused"
AWAITING_REVIEW = "Awaiting-Review"
COMPLETED = "Completed"
REJECTED = "Rejected"
CANCELLED = "Cancelled"
MERGED = "Merged"

STATES = (
	DRAFT,
	AVAILABLE,
	CLAIMED,
	IN_PROGRESS,
	PAUSED,
	AWAITING_REVIEW,
	COMPLETED,
	REJECTED,
	CANCELLED,
	MERGED,
)

#: v0.79.0. PAUSED IS A STATE AND NOT A FLAG, and the reason is the board. A
#: worker irrigating who is called to a broken valve has not stopped holding the
#: irrigation job and has not finished it; a boolean beside In-Progress would
#: leave the Kanban board showing two tasks in the same column with one of them
#: not being worked, which is the picture a dispatch board exists to prevent.
#: The clock stops here — see `Task Time Segment`.
#:
#: MERGED IS TERMINAL AND NAMES ANOTHER RECORD. Two people who independently
#: raised a job about the same valve leave two tasks, and folding one into the
#: other must not delete it: the duplicate carries somebody's photographs, their
#: minutes and their account of what they found. It goes to Merged with
#: `merged_into` pointing at where the work actually went.

#: States in which the work is finished or abandoned. Nothing claims, starts,
#: completes or rejects out of one of these.
TERMINAL_STATES = (COMPLETED, REJECTED, CANCELLED, MERGED)

#: States that hold a worker's name and count against their concurrent-claim
#: limit. Awaiting-Review deliberately does NOT: the worker has finished and the
#: task is somebody else's problem, so holding it against their limit would
#: punish them for finding something.
#:
#: PAUSED DOES COUNT. A paused task is still that worker's to come back to, and
#: a limit that ignored them would let somebody pause their way through the whole
#: pool — which is the exact hoarding the limit exists to stop, performed one
#: interruption at a time.
LIVE_ASSIGNMENT_STATES = (CLAIMED, IN_PROGRESS, PAUSED)

#: The one state a worker may be in on more than one task at once is NOT this
#: one. Being in two places is not a scheduling preference; `pause_farm_task`
#: and the auto-pause on claim/start are what make that true without refusing
#: anybody's second job.
EXCLUSIVE_STATES = (IN_PROGRESS,)

#: How many tasks one worker may hold at once. Three is a morning: enough to
#: plan a trip round the camp, few enough that nobody can empty the pool onto
#: their own name and leave the board looking worked. It is a hoarding limit,
#: not a productivity one — completing one frees a slot immediately.
MAX_CONCURRENT_CLAIMS = 3

#: The evidence contract's vocabulary, and what each key obliges a completion to
#: carry. A key outside this set is REFUSED rather than ignored: `{"photo": true}`
#: where `"photos"` was meant asks for nothing and looks like it asks for
#: something, which is the worst of both.
EVIDENCE_KEYS = {
	"photos": "at least one photograph filed against the completion",
	"signature": "a signature capture — the worker attesting to what they found",
	"findings_text": "what they actually saw, in words, whether or not anything was wrong",
	"witness": "the name of somebody else who was there and saw the same thing",
	"gps": 'a location fix taken where the work was done, as "lat,lon"',
}

#: v0.115.0. `gps` IS ADDITIVE AND NOTHING EXISTING ASKS FOR IT. Every contract
#: already stored omits the key, `evidence_contract` reads only the keys a
#: contract actually names, and `_unmet_evidence` checks only what it was asked
#: for — so no task already on a board tightened when this shipped.
#:
#: IT EXISTS BECAUSE A SCOUTING ROUND IS ABOUT A PLACE. A cabin inspection is
#: about a cabin and the docname says which; a block is twenty acres and an
#: observation of "the northwest corner" cannot be compared to next week's round
#: of the same corner. The handset captures the fix without anybody being asked
#: for it, which is exactly why it needs to be in the contract: evidence nobody
#: has to type is evidence nobody notices is missing.

#: The dispatch modes, and which of them a worker may claim from the pool.
DISPATCH_EITHER = "Either"
DISPATCH_DISPATCHED = "Dispatched"
DISPATCH_SELF_PICK = "Self-pick"
SELF_PICKABLE = (DISPATCH_EITHER, DISPATCH_SELF_PICK)

#: How a task came into being. The default is compliance_rule for backwards
#: compatibility — every task that existed before v0.23.0 was either generated
#: from a compliance alert or raised by a foreman, and compliance_rule is the
#: right default for the majority that were generated.
ORIGIN_COMPLIANCE_RULE = "compliance_rule"
ORIGIN_FOREMAN_DISPATCH = "foreman_dispatch"
ORIGIN_FIELD_REPORTED = "field_reported"
ORIGIN_WORKER_SELF_PICK = "worker_self_pick_from_pool"

ORIGINS = (
	ORIGIN_COMPLIANCE_RULE,
	ORIGIN_FOREMAN_DISPATCH,
	ORIGIN_FIELD_REPORTED,
	ORIGIN_WORKER_SELF_PICK,
)

#: v0.41.0. The keys one snapshotted checklist item carries. The first four are
#: copied off the Farm Task Template at creation and never change afterwards —
#: that snapshot is what makes editing a template safe — and the last two are
#: the worker's own answer, written by `complete_farm_task`.
CHECKLIST_ITEM_KEYS = ("item_name", "required", "evidence_type", "sort_order", "done", "note")


class FarmTask(Document):
	def autoname(self):
		month = str(frappe.utils.today())[:7]
		prefix = f"FT-{month}-"
		existing = frappe.db.get_all(
			"Farm Task",
			filters={"name": ("like", f"{prefix}%")},
			pluck="name",
			limit=100000,
		)
		highest = 0
		for name in existing or []:
			tail = str(name).rsplit("-", 1)[-1]
			if tail.isdigit():
				highest = max(highest, int(tail))
		self.name = f"{prefix}{highest + 1:05d}"

	def validate(self):
		self.task_name = str(self.task_name or "").strip()
		if not self.task_name:
			frappe.throw(_("Task is required — a task nobody can name is a task nobody will do."))
		if not self.task_type:
			frappe.throw(_("Task Type is required."))

		self.state = str(self.state or DRAFT).strip() or DRAFT
		if self.state not in STATES:
			frappe.throw(
				_("State {0} is not one of: {1}.").format(self.state, ", ".join(STATES)),
				title=_("Unknown Farm Task state"),
			)

		self.origin = str(self.origin or ORIGIN_COMPLIANCE_RULE).strip() or ORIGIN_COMPLIANCE_RULE
		if self.origin not in ORIGINS:
			frappe.throw(
				_("Origin {0} is not one of: {1}.").format(self.origin, ", ".join(ORIGINS)),
				title=_("Unknown Farm Task origin"),
			)

		if int(self.estimated_duration_minutes or 0) < 0:
			frappe.throw(_("Estimated Duration cannot be negative."))

		self.evidence_required = json.dumps(parse_evidence_required(self.evidence_required))
		self.creates_record_data = json.dumps(
			parse_json_object(self.creates_record_data, "Creates Record Data")
		)
		self.checklist_status = json.dumps(parse_checklist_status(self.checklist_status))

		self.assigned_to = str(self.assigned_to or "").strip()
		self.assigned_to_name = str(self.assigned_to_name or "").strip() or self.assigned_to

		if self.location and not self.location_doctype:
			frappe.throw(
				_(
					"Location {0} was given with no Location DocType, so nothing can resolve it. "
					"A Dynamic Link needs both halves."
				).format(self.location)
			)

		self.company = self.company or _company_of(self.location_doctype, self.location)


def parse_evidence_required(raw) -> dict:
	"""The evidence contract as a dict of booleans, or a refusal saying why.

	Refuses a blank contract, a contract that is not an object, a key outside
	`EVIDENCE_KEYS`, and a contract that requires nothing at all. The last one is
	the interesting refusal: `{}` is well-formed JSON and would create a task
	anybody could close by saying they had done it, which is precisely the shape
	of record this whole doctype exists to stop being written.
	"""
	value = parse_json_object(raw, "Evidence Required")
	if not value:
		frappe.throw(
			_(
				"Evidence Required is empty. Say what closing this task obliges somebody to "
				"produce — one or more of: {0}. A task that requires no evidence is a task "
				"that gets closed with a tick in a box, and a tick in a box is what an "
				"auditor is trained to disbelieve."
			).format(", ".join(sorted(EVIDENCE_KEYS))),
			title=_("Evidence Required is required"),
		)

	unknown = sorted(set(value) - set(EVIDENCE_KEYS))
	if unknown:
		frappe.throw(
			_(
				"Evidence Required names {0}, which nothing checks. The keys are: {1}. A "
				"misspelt key asks for nothing, refuses nothing, and looks exactly like a "
				"requirement right up until somebody reads the record."
			).format(", ".join(repr(key) for key in unknown), ", ".join(sorted(EVIDENCE_KEYS))),
			title=_("Unknown evidence key"),
		)

	out = {key: bool(value.get(key)) for key in sorted(value)}
	if not any(out.values()):
		frappe.throw(
			_(
				"Evidence Required has every requirement switched off, which is the same as "
				"asking for nothing. Set at least one of {0} to true."
			).format(", ".join(sorted(EVIDENCE_KEYS)))
		)
	return out


def parse_checklist_status(raw) -> dict:
	"""The snapshotted checklist and its ticks, or a refusal saying why.

	v0.41.0. THE SHAPE IS `{"items": [...]}` AND NOT A BARE LIST, because a bare
	list has nowhere to put the next thing anybody wants on it — who ticked an
	item, when — and a field whose shape changes in a later release is one every
	client has to handle twice.

	AN EMPTY CHECKLIST IS THE ORDINARY CASE and is not refused. That is the whole
	difference between this field and `evidence_required`, which is mandatory: an
	evidence contract states what closing a task PRODUCES, and every task produces
	something checkable; a checklist states how the work is subdivided, and most
	work is not. A one-item checklist saying 'do the task' is a form somebody
	learns to tick without reading, which is worse than no form.

	What it refuses is a list whose items are unusable: not an object, no name, a
	duplicate name — the same three refusals the template's own controller makes,
	restated here because a snapshot can arrive from a client rather than from a
	template.
	"""
	value = parse_json_object(raw, "Checklist Status")
	if not value:
		return {"items": []}

	unknown = sorted(set(value) - {"items"})
	if unknown:
		frappe.throw(
			_("Checklist Status names {0}, which nothing reads. The only key is 'items'.").format(
				", ".join(repr(key) for key in unknown)
			),
			title=_("Unknown checklist key"),
		)

	raw_items = value.get("items") or []
	if not isinstance(raw_items, list):
		frappe.throw(
			_("Checklist Status 'items' must be a JSON list, got {0}.").format(type(raw_items).__name__),
			title=_("Malformed checklist"),
		)

	items = []
	seen: dict = {}
	for index, entry in enumerate(raw_items):
		if not isinstance(entry, dict):
			frappe.throw(
				_("Checklist item {0} is a {1}, not an object.").format(index + 1, type(entry).__name__),
				title=_("Malformed checklist"),
			)
		name = str(entry.get("item_name") or "").strip()
		if not name:
			frappe.throw(
				_(
					"Checklist item {0} has no item_name. The name is what a completion marks done, "
					"so an unnamed item is one nobody can ever tick and nobody can ever close a "
					"task past."
				).format(index + 1),
				title=_("Unnamed checklist item"),
			)
		key = name.casefold()
		if key in seen:
			frappe.throw(
				_(
					"Checklist items {0} and {1} are both called {2}. The item name is the key a "
					"completion marks, so two of them mean a tick lands on whichever was read first."
				).format(seen[key], index + 1, repr(name)),
				title=_("Duplicate checklist item"),
			)
		seen[key] = index + 1
		items.append(
			{
				"item_name": name,
				"required": bool(entry.get("required", True)),
				"evidence_type": str(entry.get("evidence_type") or "None").strip() or "None",
				"sort_order": int(entry.get("sort_order") or index + 1),
				"done": bool(entry.get("done")),
				"note": str(entry.get("note") or "").strip(),
			}
		)
	return {"items": items}


def checklist_items(raw) -> list:
	"""The snapshotted checklist, tolerant of a stored value nobody can now parse.

	The READ side, exactly as `evidence_contract` is to `parse_evidence_required`:
	a document that somehow holds bad JSON should be reported rather than made
	unreadable. The write side goes through `parse_checklist_status`, which refuses.
	"""
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
	except Exception:
		return []
	if not isinstance(value, dict):
		return []
	items = value.get("items")
	if not isinstance(items, list):
		return []
	return [dict(entry) for entry in items if isinstance(entry, dict)]


def unmet_checklist(raw) -> list:
	"""The names of the REQUIRED checklist items nobody has marked done.

	v0.41.0, and it is what `complete_farm_task` refuses on. An OPTIONAL item left
	undone is not reported: that is what optional means, and a template that
	covers more than today needs stays usable precisely because the propane check
	on a cabin with no propane can be left rather than falsely ticked.
	"""
	return [
		str(item.get("item_name") or "")
		for item in checklist_items(raw)
		if item.get("required", True) and not item.get("done")
	]


def parse_json_object(raw, label: str) -> dict:
	"""A JSON object from a string, a dict, or nothing. Anything else is refused."""
	if raw in (None, ""):
		return {}
	if isinstance(raw, dict):
		return dict(raw)
	try:
		value = json.loads(raw)
	except Exception:
		frappe.throw(
			_("{0} is not valid JSON: {1}").format(label, str(raw)[:200]),
			title=_("Malformed JSON"),
		)
	if not isinstance(value, dict):
		frappe.throw(
			_("{0} must be a JSON object, got {1}.").format(label, type(value).__name__),
			title=_("Malformed JSON"),
		)
	return value


def evidence_contract(raw) -> dict:
	"""The evidence contract, tolerant of a stored value nobody can now parse.

	Used on the READ side, where a document that somehow holds bad JSON should be
	reported rather than made unreadable. The write side goes through
	`parse_evidence_required`, which refuses.
	"""
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
	except Exception:
		return {}
	if not isinstance(value, dict):
		return {}
	return {key: bool(value.get(key)) for key in EVIDENCE_KEYS if key in value}


def _company_of(location_doctype: str, location: str) -> str | None:
	"""The company a location belongs to, where the register knows.

	Every location register in this app names its company differently — a Parcel
	and a Housing Unit have `owning_entity`, a Certification has `company` — so
	this asks for whichever the doctype actually has rather than assuming.
	"""
	if not location_doctype or not location:
		return None
	for fieldname in ("owning_entity", "company"):
		try:
			if not frappe.get_meta(location_doctype).has_field(fieldname):
				continue
			value = frappe.db.get_value(location_doctype, location, fieldname)
		except Exception:
			continue
		if value:
			return value
	return None

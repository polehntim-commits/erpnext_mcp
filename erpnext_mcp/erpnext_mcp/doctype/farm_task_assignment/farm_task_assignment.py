# SPDX-License-Identifier: MIT
"""Controller for Farm Task Assignment — who took the work, and what they found.

WHY THIS IS A SEPARATE DOCTYPE AND NOT A CHILD TABLE OF FARM TASK. The Sprint 8
specification left the choice open and asked for whichever shape survives the
class of bug v0.14.0 found in chunked uploads. It is the same arithmetic and it
comes out the same way.

Frappe rewrites a document's ENTIRE child table on every save of the parent. A
task's assignment history is short — claimed, rejected, reclaimed, started,
finished — so the quadratic term is small, and that is not what decides it.
What decides it is the query the dispatch board runs constantly:

    everything worker 42 is currently holding

Against a child table that is a scan of every Farm Task on the site, unnesting
every history, filtering in Python — because a child row's parent is the only
indexed way in and the worker is not the parent. Against a doctype it is one
indexed read on `assigned_to`. The concurrent-claim limit asks that question on
EVERY claim, by every worker, all morning.

The second reason is writes. Claim, start and complete each touch one assignment.
As a child table each of those rewrites the whole task document and every row
under it, so two workers finishing two different tasks contend on nothing, but a
foreman editing a task's instructions while somebody is completing it silently
discards one of the two. As a doctype the assignment and the task are separate
rows and the race does not exist.

`evidence_files` IS A CHILD TABLE, and that is consistent rather than contrary:
it is written once at completion, holds a handful of rows, and is only ever read
with its parent.

THE NAME IS `FTA-YYYY-MM-<seq>`, sequenced within the month from the rows already
there, for the same reason Housing Assignment's is.

ONE LIVE ASSIGNMENT PER TASK. A task can have many assignments over its life —
that is the point of rejection being a first-class state — but only one that is
Claimed or In-Progress, because two people stood in the same cabin both believing
it is theirs is the failure this whole system exists to prevent. Enforced here as
well as in the tools, so the Desk cannot open a second door.

NOTHING HERE IS EVER DELETED. A rejected assignment is the record that somebody
was sent, looked, and could not do it — which is a considerably more useful
compliance answer than an absence.
"""

import frappe
from frappe import _
from frappe.model.document import Document

CLAIMED = "Claimed"
IN_PROGRESS = "In-Progress"
PAUSED = "Paused"
COMPLETED = "Completed"
REJECTED = "Rejected"
MERGED = "Merged"
#: v0.213.0 (docs/design/crew_tasks.md). One worker's stint on a CREW task: on
#: the job, and off it. Deliberately NOT live states — the lead holds the task,
#: and the one-live-assignment rule, the claim limit and "what is this worker
#: holding" must not see the twelve people doing the work.
ON_CREW = "On Crew"
OFF_CREW = "Off Crew"

STATES = (CLAIMED, IN_PROGRESS, PAUSED, COMPLETED, REJECTED, MERGED, ON_CREW, OFF_CREW)

#: The states in which this assignment is the one that owns the task.
#:
#: v0.79.0 ADDS PAUSED, and it has to be here rather than beside the finished
#: states: a paused assignment is still the one holding the task, so
#: `live_assignment` must find it — otherwise `resume_farm_task` would be told
#: nobody is holding the job it is trying to pick back up, and a second worker
#: could claim a task somebody is coming back to.
LIVE_STATES = (CLAIMED, IN_PROGRESS, PAUSED)


class FarmTaskAssignment(Document):
	def autoname(self):
		month = str(frappe.utils.today())[:7]
		prefix = f"FTA-{month}-"
		existing = frappe.db.get_all(
			"Farm Task Assignment",
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
		if not self.task:
			frappe.throw(_("Task is required."))
		self.assigned_to = str(self.assigned_to or "").strip()
		if not self.assigned_to:
			frappe.throw(
				_("Assigned To is required — an assignment with no name on it is not an assignment.")
			)
		self.assigned_to_name = str(self.assigned_to_name or "").strip() or self.assigned_to

		self.state = str(self.state or CLAIMED).strip() or CLAIMED
		if self.state not in STATES:
			frappe.throw(_("State {0} is not one of: {1}.").format(self.state, ", ".join(STATES)))

		if self.state == REJECTED and not str(self.rejection_reason or "").strip():
			frappe.throw(
				_(
					"A rejection needs a reason. 'Nobody got to it' is the answer that cannot be "
					"defended six months later; 'the ladder is broken and I could not reach the "
					"detector' is a fact somebody can act on."
				),
				title=_("Rejection Reason is required"),
			)

		if self.started_at and self.claimed_at and str(self.started_at) < str(self.claimed_at):
			frappe.throw(_("Started {0} is before Claimed {1}.").format(self.started_at, self.claimed_at))
		if self.completed_at and self.started_at and str(self.completed_at) < str(self.started_at):
			frappe.throw(_("Completed {0} is before Started {1}.").format(self.completed_at, self.started_at))

		if int(self.actual_duration_minutes or 0) < 0:
			frappe.throw(_("Actual Duration cannot be negative."))

		if int(self.pause_count or 0) < 0:
			frappe.throw(_("Pause Count cannot be negative."))
		if self.state == PAUSED and not self.paused_at:
			frappe.throw(
				_(
					"A paused assignment has to say WHEN it was paused. Without the timestamp the "
					"segment that stopped has no end, and the hour charged to this job becomes the "
					"wall clock across an interruption."
				),
				title=_("Paused At is required"),
			)

		self.task_name = self.task_name or frappe.db.get_value("Farm Task", self.task, "task_name")
		self.company = self.company or frappe.db.get_value("Farm Task", self.task, "company")

		if self.state in LIVE_STATES:
			self._refuse_a_second_live_assignment()
		if self.state == ON_CREW:
			self._refuse_the_same_person_twice()

	def _refuse_the_same_person_twice(self) -> None:
		other = frappe.db.get_value(
			"Farm Task Assignment",
			{
				"task": self.task,
				"state": ON_CREW,
				"assigned_to": self.assigned_to,
				"name": ("!=", self.name or ""),
			},
			"name",
		)
		if other:
			frappe.throw(
				_("{0} is already on the crew of {1} ({2}).").format(
					self.assigned_to_name or self.assigned_to, self.task, other
				),
				title=_("Already on the crew"),
			)

	def _refuse_a_second_live_assignment(self) -> None:
		other = frappe.db.get_value(
			"Farm Task Assignment",
			{
				"task": self.task,
				"state": ("in", list(LIVE_STATES)),
				"name": ("!=", self.name or ""),
			},
			["name", "assigned_to_name"],
			as_dict=True,
		)
		if other:
			frappe.throw(
				_(
					"{0} is already held by {1} ({2}). One live assignment per task — two people "
					"stood in front of the same work both believing it is theirs is the thing a "
					"dispatch board exists to prevent. They have to reject it, or a foreman has "
					"to reassign it."
				).format(self.task, other.get("assigned_to_name") or other.get("name"), other["name"]),
				title=_("Task is already claimed"),
			)


def live_assignment(task: str):
	"""The assignment currently holding this task, or None."""
	name = frappe.db.get_value(
		"Farm Task Assignment",
		{"task": task, "state": ("in", list(LIVE_STATES))},
		"name",
	)
	return name or None


def concurrent_claims(worker: str) -> list:
	"""Every task this worker is holding right now, as assignment docnames.

	Claimed and In-Progress only. A completion frees the slot immediately, and an
	Awaiting-Review task is deliberately not counted: the worker has finished and
	somebody else has to look, so holding it against their limit would punish them
	for finding something.
	"""
	worker = str(worker or "").strip()
	if not worker:
		return []
	return [
		str(row)
		for row in frappe.db.get_all(
			"Farm Task Assignment",
			filters={"assigned_to": worker, "state": ("in", list(LIVE_STATES))},
			pluck="name",
			limit=200,
		)
		or []
	]

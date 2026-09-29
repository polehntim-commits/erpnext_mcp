# SPDX-License-Identifier: MIT
"""Controller for Farm Task Template — the shape of one recurring job, as data.

WHY THIS EXISTS WHEN THERE IS ALREADY AN INSPECTION TEMPLATE. They are different
sizes of thing and the difference is load-bearing. An Inspection Template is a
MULTI-SECTION VISIT: one trip to one cabin producing a Housing Inspection, two
Detector Tests and a Water Test, at four separate regulated cadences, out of one
claim. A Farm Task Template is the shape of ONE TASK. Collapsing them would make
every smoke detector test carry a sections table with a single row in it, and
would make `sections[0].produces_record_doctype` the answer to a question the
Farm Task already has a field for. They share a vocabulary — the evidence
contract keys, the skill strings, the regime tags — and nothing else.

────────────────────────────────────────────────────────────────────────────
IT IS EDITED IN PLACE, AND THAT IS A DEPARTURE THIS FILE HAS TO DEFEND
────────────────────────────────────────────────────────────────────────────

An Inspection Template is versioned by copy. A Compliance Rule is versioned by
copy. This one is not, and the reason is what reads back from each of them:

  * a SESSION reads its template's sections at submission time, weeks after it
    was started, so the row it started against has to still exist unchanged or
    the form it submits is not the form the worker saw;
  * an ALERT is read against the rule that raised it, so a threshold edited in
    July would retroactively change what April's alert was claiming;
  * a TASK reads its template EXACTLY ONCE, at creation, and copies everything
    it needs onto its own fields. `create_task_from_template` snapshots
    `task_type`, `skill_required`, `estimated_duration_minutes`, `dispatch_mode`,
    `evidence_required`, `creates_record`, `creates_record_data`, `instructions`
    and the whole checklist. After that the task is self-contained: it can be
    claimed, worked, evidenced and closed with this record deleted.

So there is no live document whose meaning an edit could change underneath it,
and versioning by copy would buy an audit trail Frappe's own `track_changes`
already keeps — at the cost of a register in which the eight templates an
operation actually runs are lost among forty superseded rows. Changing a
template changes what FUTURE tasks look like. That is the whole rule.

────────────────────────────────────────────────────────────────────────────
WHAT IT REFUSES
────────────────────────────────────────────────────────────────────────────

  * a missing or nonsense EVIDENCE CONTRACT — for exactly the reason Farm Task
    refuses one. A template with no contract raises tasks with no contract, and
    Farm Task will not insert those, so an optional field here would be a
    template that saves happily and fails the first time somebody stood in a
    cabin tries to use it;
  * two CHECKLIST ITEMS with one name. The item name is the key a completion
    marks, so duplicates mean a tick lands on whichever row was read first and
    the other stays open for reasons nobody can see;
  * a checklist item with no name at all;
  * a NEGATIVE DURATION.

WHAT IT DOES NOT REFUSE, ON PURPOSE:

  * a `creates_record` naming a DocType this site has not got. This app installs
    doctypes over time and an operation may template work against a record a
    later release adds; refusing here would make the template unsaveable today
    for a spelling that will be right in a month. The refusal that matters is at
    TASK CREATION, where `create_farm_task` already raises it — with a worker
    about to be sent somewhere, which is the moment it is worth stopping;
  * a template with no checklist. Most should have none.
  * a template with a checklist and no required item. A list of optional prompts
    is a legitimate aide-mémoire, and refusing it would push operators into
    marking everything required to get the record saved, which is how a required
    flag stops meaning anything.
"""

import json

import frappe
from frappe import _
from frappe.model.document import Document

from ..farm_task.farm_task import parse_evidence_required, parse_json_object

#: The evidence a checklist item asks the worker to capture beside the tick.
#: A HINT the handset draws from, never a second refusal — see the DocType
#: description on `evidence_type` for why there is only one evidence gate.
EVIDENCE_TYPES = ("None", "Photo", "Text", "Measurement")


class FarmTaskTemplate(Document):
	def validate(self):
		self.template_name = str(self.template_name or "").strip()
		if not self.template_name:
			frappe.throw(_("Template Name is required — a template nobody can name is one nobody will find."))
		if not self.task_type:
			frappe.throw(_("Task Type is required."))

		if int(self.estimated_duration_minutes or 0) < 0:
			frappe.throw(_("Estimated Duration cannot be negative."))

		# The same parser Farm Task uses, so a contract that would be refused on
		# the task is refused HERE — while the person who can fix it is looking at
		# what they typed, rather than on the first morning somebody needed the work.
		self.evidence_required = json.dumps(parse_evidence_required(self.evidence_required))
		self.creates_record_data = json.dumps(
			parse_json_object(self.creates_record_data, "Creates Record Data")
		)
		self.creates_record = str(self.creates_record or "").strip()
		self.skill_required = str(self.skill_required or "").strip()

		self._check_the_checklist()
		self._bump_version()

	def _bump_version(self) -> None:
		"""v0.207.0: a new version whenever what a task snapshots changes, so every
		task can say which shape it was raised from (Farm Task.template_version)."""
		import hashlib

		shape = json.dumps(
			[
				self.get("form_schema") or "",
				[(r.item_name, r.evidence_type) for r in self.checklist or []],
				self.evidence_required or "",
				self.get("required_certification") or "",
			],
			sort_keys=True,
			default=str,
		)
		digest = hashlib.sha256(shape.encode()).hexdigest()[:16]
		if not self.get("version"):
			self.version = 1
		elif self.get("form_hash") and self.form_hash != digest:
			self.version = int(self.version) + 1
		self.form_hash = digest

	def _check_the_checklist(self) -> None:
		"""Names that are present and distinct, and an order that is a sequence."""
		seen: dict = {}
		for index, row in enumerate(self.checklist or []):
			name = str(row.item_name or "").strip()
			if not name:
				frappe.throw(
					_(
						"Checklist item {0} has no name. The name is what a completion marks done, "
						"so an unnamed item is one nobody can ever tick and nobody can ever close "
						"a task past."
					).format(index + 1),
					title=_("Unnamed checklist item"),
				)
			key = name.casefold()
			if key in seen:
				frappe.throw(
					_(
						"Checklist items {0} and {1} are both called {2}. The item name is the KEY a "
						"completion names, so two of them mean a tick lands on whichever was read "
						"first and the other stays open for reasons nobody can see."
					).format(seen[key], index + 1, repr(name)),
					title=_("Duplicate checklist item"),
				)
			seen[key] = index + 1
			row.item_name = name
			row.evidence_type = str(row.evidence_type or "None").strip() or "None"
			if row.evidence_type not in EVIDENCE_TYPES:
				frappe.throw(
					_("Checklist item {0} asks for evidence type {1}, which is not one of: {2}.").format(
						name, repr(row.evidence_type), ", ".join(EVIDENCE_TYPES)
					),
					title=_("Unknown evidence type"),
				)
			if row.sort_order in (None, "", 0):
				row.sort_order = index + 1


def checklist_rows(name: str) -> list:
	"""One template's checklist as plain dicts, in worked order.

	Ordered by `sort_order` and then by `idx`, so a template whose orders were
	typed by hand and left with ties still reads the same way on every site — a
	checklist whose order depends on which row the database returned first is a
	checklist that reorders itself between two people looking at it.
	"""
	rows = frappe.db.get_all(
		"Farm Task Template Checklist Item",
		filters={"parent": name, "parenttype": "Farm Task Template"},
		fields=["item_name", "required", "evidence_type", "sort_order", "idx"],
		limit=500,
	)
	ordered = sorted(
		[dict(row) for row in rows or []],
		key=lambda row: (int(row.get("sort_order") or 0), int(row.get("idx") or 0)),
	)
	return [
		{
			"item_name": str(row.get("item_name") or ""),
			"required": bool(row.get("required")),
			"evidence_type": str(row.get("evidence_type") or "None"),
			"sort_order": int(row.get("sort_order") or 0),
		}
		for row in ordered
	]

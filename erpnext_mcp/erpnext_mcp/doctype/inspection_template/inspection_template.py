# SPDX-License-Identifier: MIT
"""Controller for Inspection Template — the shape of a visit, as a record.

WHAT IT REFUSES, AND WHY EACH REFUSAL EARNED ITS PLACE:

  * A TEMPLATE WITH NO SECTIONS. That is a name. The whole content of a template
    is what a worker is asked to do, and a row without it would reach a handset
    as an empty form.
  * TWO SECTIONS SHARING A NAME. The section name is the KEY a submission
    matches on — `submit_inspection_session` looks the section up by it — so two
    sections called "Detector Test" would make one of the two unsubmittable, and
    which one would depend on iteration order.
  * A SECOND LIVE TEMPLATE WITH THE SAME NAME. Enforced here rather than by a
    unique index, because the constraint is not on the column: version 1 and
    version 2 share a name by construction, and superseding is the mechanism by
    which history stays readable. What may not exist twice is a template that is
    active AND unsuperseded, because those are two answers to "start a Mid-season
    Habitability here" and the one a worker gets would be whichever sorted first.
  * A MALFORMED JSON BLOB, or a contract key outside the vocabulary. `{"photo":
    true}` where `"photos"` was meant asks for nothing and looks like it asks
    for something.
  * A `produces_record_doctype` NAMING A DOCTYPE THIS SITE HAS NOT GOT. A section
    pointing at a doctype that does not exist refuses every submission at the
    moment a worker is standing in a cabin trying to file one, which is the worst
    possible time to discover a typo. Refused here, at authoring time, when the
    person who can fix it is the person present.

WHAT IT DOES NOT REFUSE. A template whose sections produce nothing at all — a
close-down is mostly photographs and nobody regulates a photograph of an emptied
refrigerator as its own document. A template nobody has tagged with a regime. A
template with one section. Every one of those is a real shape of visit, and a
controller that refused them would guarantee the operator kept their checklist in
a spreadsheet instead.

THE ORDER INDEX IS FILLED IN, NEVER DEMANDED. An author who typed the sections in
the order they are worked has already said what the order is.
"""

import json

import frappe
from frappe import _
from frappe.model.document import Document

from erpnext_mcp import sessions, shifts


class InspectionTemplate(Document):
	def autoname(self):
		"""INSPT-YYYY-0001, where YYYY is the year the template was authored."""
		year = str(frappe.utils.today())[:4]
		self.name = shifts.next_in_series(sessions.TEMPLATE_DOCTYPE, "INSPT", year)

	def validate(self):
		self.template_name = str(self.template_name or "").strip()
		if not self.template_name:
			frappe.throw(_("Template Name is required — a template nobody can ask for by name."))
		self.description = str(self.description or "").strip()
		if not self.description:
			frappe.throw(
				_(
					"Description is required. A template with sections and no statement of what "
					"the visit is for is a form somebody has to reverse-engineer."
				)
			)
		if not self.applies_to_asset_type:
			self.applies_to_asset_type = "General"
		if int(self.version or 0) < 1:
			self.version = 1

		self._check_the_sections()
		self._refuse_a_second_live_template()

	# ── the parts ───────────────────────────────────────────────────────────
	def _check_the_sections(self) -> None:
		rows = self.get("sections") or []
		if not rows:
			frappe.throw(
				_("A template needs at least one section. A template with none is a name."),
				title=_("No sections"),
			)
		if len(rows) > sessions.SECTION_CAP:
			frappe.throw(
				_(
					"{0} sections, past the {1} cap. A visit with more parts than that is not a "
					"visit, it is a day — and a form nobody finishes is worse than three forms."
				).format(len(rows), sessions.SECTION_CAP)
			)

		seen = set()
		for index, row in enumerate(rows):
			name = str(row.get("section_name") or "").strip()
			if not name:
				frappe.throw(_("Section {0} has no name.").format(index + 1))
			row.section_name = name
			key = name.casefold()
			if key in seen:
				frappe.throw(
					_(
						"Two sections are both called {0}. The section name is the key a "
						"submission names, so one of the two could never be submitted."
					).format(name),
					title=_("Duplicate section"),
				)
			seen.add(key)

			if not int(row.get("order_index") or 0):
				row.order_index = index + 1

			hint = str(row.get("renderer_hint") or "").strip()
			row.renderer_hint = hint or "checklist"

			produces = str(row.get("produces_record_doctype") or "").strip()
			if produces and not frappe.db.exists("DocType", produces):
				frappe.throw(
					_(
						"Section {0} says it produces a {1}, which is not a DocType on this site. "
						"A section pointing at a doctype that does not exist refuses every "
						"submission — and it does it while somebody is standing in a cabin trying "
						"to file one. Leave it empty for a section that gathers evidence and "
						"produces no standalone record."
					).format(name, produces),
					title=_("No such DocType"),
				)

			for fieldname, label in (
				("evidence_contract_json", "evidence_contract_json"),
				("produces_record_data_json", "produces_record_data_json"),
				("field_prompts_json", "field_prompts_json"),
			):
				try:
					parsed = (
						sessions.as_prompts(row.get(fieldname), f"{name}.{label}")
						if fieldname == "field_prompts_json"
						else sessions.as_object(row.get(fieldname), f"{name}.{label}")
					)
				except ValueError as exc:
					frappe.throw(str(exc), title=_("Malformed JSON"))
				if fieldname == "evidence_contract_json":
					try:
						parsed = sessions.check_contract(parsed, f"{name}.evidence_contract")
					except ValueError as exc:
						frappe.throw(str(exc), title=_("Unknown contract key"))
				row.set(fieldname, json.dumps(parsed) if parsed else "{}")

	def _refuse_a_second_live_template(self) -> None:
		"""One LIVE row per template name. See the module docstring for why here."""
		if not int(self.active or 0) or str(self.superseded_by or "").strip():
			return
		clash = frappe.db.get_all(
			sessions.TEMPLATE_DOCTYPE,
			filters={
				"template_name": self.template_name,
				"active": 1,
				"superseded_by": ("in", ("", None)),
				"name": ("!=", self.name or "new"),
			},
			pluck="name",
			limit=1,
		)
		if clash:
			frappe.throw(
				_(
					"{0} is already a live template ({1}). Two active, unsuperseded templates "
					"with one name are two answers to the same question and a worker would get "
					"whichever sorted first. Edit that one with update_inspection_template — it "
					"supersedes rather than overwrites, so the version already worked from stays "
					"readable — or deactivate it first."
				).format(self.template_name, clash[0]),
				title=_("Template name is taken"),
			)

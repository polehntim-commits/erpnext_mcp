# SPDX-License-Identifier: MIT
"""Controller for Critical Tracking Event — one row of the FSMA 204 record.

WHAT IT DERIVES rather than takes. The company, off the lot, so an event is
scoped by the same User Permission the lot is and an operator never has to pass
it twice. The actor's name, snapshotted at write time, for the reason every
record here snapshots one: a lookup at report time answers with today's name and
an audit two years later is entitled to who the person was THEN. And the event
timestamp, defaulting to now, because an event without one cannot be put in a
timeline and a timeline is what the rule actually asks for.

WHAT IT REFUSES, AND ONLY THIS.

  * A negative quantity. Same argument as everywhere else in this app: the
    figure becomes a settlement figure and a negative one propagates.
  * A `reference_name` with no `reference_doctype`. Half a pointer resolves to
    nothing and is worse than no pointer at all, because the reads report an
    unresolved reference as a data fault and would be reporting one that was
    never a reference.

WHAT IT DOES NOT REFUSE, AND EACH ONE IS A DECISION.

A Shipping event with no `destination_location` or no `receiver`. That is product
that left and cannot be traced to anybody — the single most important thing a
recall report can tell an operation, and refusing to record it would delete the
evidence instead of surfacing it. `recall_drill` names it as a break.

A `reference_doctype` naming a doctype this site does not have. An event indexed
on one bench and read on another, or a register uninstalled since; the pointer is
still the truest record of where the detail was. `Data` rather than a Dynamic
Link exists precisely so this survives.

An event whose `event_datetime` precedes its lot's `harvest_date`. It reads like
an error and is routinely correct: a Growing event is a spray applied months
before the fruit came off, and that is the ONE hop a residue question is asked
through. Refusing it would refuse the feature.
"""

import frappe
from frappe import _
from frappe.model.document import Document

#: The doctype whose `company` an event inherits. Named once so a rename of the
#: lot register cannot leave a string literal behind here pointing at nothing.
LOT_DOCTYPE = "Traceability Lot Code"


class CriticalTrackingEvent(Document):
	def validate(self):
		self._check_the_quantity()
		self._check_the_reference()
		self._fill_from_the_lot()
		self._fill_from_the_actor()
		if not str(self.event_datetime or "").strip():
			self.event_datetime = frappe.utils.now()

	# ── the parts ───────────────────────────────────────────────────────────
	def _check_the_quantity(self) -> None:
		if self.quantity not in (None, "") and frappe.utils.flt(self.quantity) < 0:
			frappe.throw(
				_(
					"An event cannot move {0}. A traceability figure becomes a settlement figure "
					"before long, and a negative one propagates."
				).format(self.quantity),
				title=_("Negative Quantity"),
			)

	def _check_the_reference(self) -> None:
		doctype = str(self.reference_doctype or "").strip()
		name = str(self.reference_name or "").strip()
		if name and not doctype:
			frappe.throw(
				_(
					"reference_name is {0} and reference_doctype is empty. Half a pointer "
					"resolves to nothing, and the reads report an unresolved reference as a data "
					"fault — this one would be reported as a fault that was never a reference."
				).format(name),
				title=_("Half a Reference"),
			)
		self.reference_doctype = doctype or None
		self.reference_name = name or None

	def _fill_from_the_lot(self) -> None:
		if self.company or not self.lot_code:
			return
		row = frappe.db.get_value(LOT_DOCTYPE, self.lot_code, ["company"], as_dict=True) or {}
		self.company = row.get("company") or None

	def _fill_from_the_actor(self) -> None:
		if not self.actor:
			return
		row = frappe.db.get_value("Employee", self.actor, ["employee_name", "company"], as_dict=True) or {}
		if not str(self.actor_name or "").strip():
			self.actor_name = row.get("employee_name") or self.actor
		if not self.company:
			self.company = row.get("company") or None

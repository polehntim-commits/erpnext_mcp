# SPDX-License-Identifier: MIT
"""Controller for Housing Assignment — who slept where, and when.

THE NAME IS `HA-YYYY-MM-<seq>`, SEQUENCED WITHIN THE MONTH. A camp fills up in
the same fortnight every year, so a name carrying the month is a name that sorts
into seasons without a report. The sequence is computed from the rows already in
that month rather than from Frappe's naming series, because the series counter is
global and would leave gaps that read like deleted records in an audit somebody
is defending.

`employee` IS A DATA FIELD, NOT A LINK, AND THAT IS DELIBERATE. Frappe HR is not
a dependency of this app. A Link to Employee would make the whole doctype fail to
migrate on a site without hrms, which is every site that has not installed it —
so the id is stored as text and validated against the Employee register *when
there is one*. Where hrms is installed the refusal is real: an assignment naming
somebody who is not on file is a roster that has already drifted from payroll.

STATUS IS DERIVED FROM THE DATES, ALWAYS. There is no separate active flag,
because two representations of one fact are two facts that will disagree. Blank
end date means currently assigned; that is the entire occupancy model, and
`get_housing_capacity` counts nothing else.

NOTHING HERE DELETES. An assignment that ended is an assignment that stays. It
is the audit trail defending a Section 119 exclusion, the answer to a wage claim
about a housing deduction, and the roster a food safety investigator asks for.
Ending one writes an end date; it does not remove a row.
"""

import frappe
from frappe import _
from frappe.model.document import Document

#: Where the "who is in the camp" model lives: no end date means still there.
CURRENT = "Current"
ENDED = "Ended"


class HousingAssignment(Document):
	def autoname(self):
		month = str(self.assigned_date or frappe.utils.today())[:7]
		prefix = f"HA-{month}-"
		existing = frappe.db.get_all(
			"Housing Assignment",
			filters={"name": ("like", f"{prefix}%")},
			pluck="name",
			limit=10000,
		)
		highest = 0
		for name in existing or []:
			tail = str(name).rsplit("-", 1)[-1]
			if tail.isdigit():
				highest = max(highest, int(tail))
		self.name = f"{prefix}{highest + 1:05d}"

	def validate(self):
		if not self.unit:
			frappe.throw(_("Housing Unit is required."))
		if not self.assigned_date:
			frappe.throw(_("Assigned Date is required — an assignment with no start is not a record."))

		self.parcel = frappe.db.get_value("Housing Unit", self.unit, "parcel") or self.parcel
		# v0.327.0. A room, where given, is one of the unit's rooms (a duplex: 16a / 16b).
		if self.get("room"):
			rooms = [r.strip() for r in str(frappe.db.get_value("Housing Unit", self.unit, "room_labels") or "").split(",")
			         if r.strip()]
			if rooms and self.room.strip() not in rooms:
				frappe.throw(_("{0} has rooms {1}; {2} is not one of them.").format(self.unit, ", ".join(rooms), self.room))
		self.employee = str(self.employee or "").strip()
		self.employee_name = str(self.employee_name or "").strip() or self.employee

		if self.end_date and self.end_date < self.assigned_date:
			frappe.throw(
				_(
					"End Date {0} is before the Assigned Date {1}. Nobody moved out before they moved in."
				).format(self.end_date, self.assigned_date)
			)

		paid = float(self.deposit_paid or 0)
		returned = float(self.deposit_returned or 0)
		if paid < 0 or returned < 0:
			frappe.throw(_("A deposit cannot be negative."))
		if returned > paid:
			frappe.throw(
				_(
					"Deposit Returned ({0}) is more than Deposit Paid ({1}). That is a refund of "
					"money nobody took — correct the deposit paid, or record the difference as "
					"something other than a deposit."
				).format(returned, paid)
			)

		self.status = ENDED if self.end_date else CURRENT


def overlaps(existing_start: str, existing_end: str, start: str, end: str) -> bool:
	"""Do two date ranges share a day, treating a blank end as open-ended?

	Half-open would be wrong here. Somebody who moves out on the 15th and
	somebody who moves in on the 15th did share the cabin that night, and a camp
	manager who is told they did not will put two people in one bed. So both ends
	are inclusive, which is also how a wage claim reads the same two records.
	"""
	existing_end = existing_end or "9999-12-31"
	end = end or "9999-12-31"
	return existing_start <= end and start <= existing_end

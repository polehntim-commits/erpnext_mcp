# SPDX-License-Identifier: MIT
"""Controller for Corrective Action Record — deviation and corrective action log.

When a monitoring record shows a critical limit was exceeded, a Corrective
Action Record documents what happened, why it happened, what was done about it,
and what product was affected. This is the record an FDA investigator asks to
see: what went wrong, and what did you do about it.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class CorrectiveActionRecord(Document):
	def validate(self):
		if not self.food_safety_plan:
			frappe.throw(_("Food Safety Plan is required."))
		if not self.preventive_control:
			frappe.throw(_("Preventive Control is required."))
		if not self.deviation_description:
			frappe.throw(_("Deviation Description is required."))
		if not self.action_taken:
			frappe.throw(_("Action Taken is required."))

		if self.deviation_date and str(self.deviation_date) > frappe.utils.today():
			frappe.throw(_("Deviation Date {0} is in the future.").format(self.deviation_date))

# SPDX-License-Identifier: MIT
"""Controller for Recall Plan — FDA recall procedures.

A Recall Plan defines who does what when a product recall is necessary. It
names the coordinator, the backup, the team contacts, the customer list, and
the FDA notification procedure. The plan should be simulated periodically to
make sure everyone knows what to do before it matters.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class RecallPlan(Document):
	def validate(self):
		if not self.recall_plan_name:
			frappe.throw(_("Recall Plan Name is required."))
		if not self.food_safety_plan:
			frappe.throw(_("Food Safety Plan is required."))
		if not self.recall_coordinator:
			frappe.throw(_("Recall Coordinator is required."))

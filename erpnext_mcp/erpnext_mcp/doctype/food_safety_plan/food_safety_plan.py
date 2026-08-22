# SPDX-License-Identifier: MIT
"""Controller for Food Safety Plan — master FSMA/HACCP plan document.

A Food Safety Plan is the top-level container for a facility's preventive
controls program under 21 CFR Part 117 or the Produce Safety Rule. It names
the qualified individual, the covered CTEs, and the plan lifecycle. Every
Hazard Analysis, Preventive Control, Monitoring Record, and Corrective Action
Record links back here.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class FoodSafetyPlan(Document):
	def validate(self):
		if not self.plan_name:
			frappe.throw(_("Plan Name is required."))
		if not self.facility_name:
			frappe.throw(_("Facility Name is required."))

		if (
			self.status == "Active"
			and self.qi_certification_expiry
			and str(self.qi_certification_expiry) < frappe.utils.today()
		):
			frappe.throw(
				_(
					"QI Certification Expiry {0} is in the past — an Active plan requires a current QI."
				).format(self.qi_certification_expiry)
			)

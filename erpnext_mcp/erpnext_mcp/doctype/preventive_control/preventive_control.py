# SPDX-License-Identifier: MIT
"""Controller for Preventive Control — CCP or preventive control with limits.

A Preventive Control defines a critical control point or other preventive
control within the HACCP system. It specifies monitoring parameters, critical
limits, corrective action procedures, and verification requirements. Monitoring
Records and Corrective Action Records link back to it to form a complete
compliance trail.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class PreventiveControl(Document):
	def validate(self):
		if not self.control_name:
			frappe.throw(_("Control Name is required."))
		if not self.food_safety_plan:
			frappe.throw(_("Food Safety Plan is required."))

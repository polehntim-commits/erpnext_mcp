# SPDX-License-Identifier: MIT
"""Controller for Verification Record — calibration, log review, product testing.

Verification activities prove the preventive controls are working as intended.
This includes equipment calibration checks, monitoring log reviews, product
testing, and sanitation verification. Each record documents what was checked,
the result, and whether the control is effective.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class VerificationRecord(Document):
	def validate(self):
		if not self.food_safety_plan:
			frappe.throw(_("Food Safety Plan is required."))
		if not self.verification_type:
			frappe.throw(_("Verification Type is required."))
		if not self.verification_date:
			frappe.throw(_("Verification Date is required."))

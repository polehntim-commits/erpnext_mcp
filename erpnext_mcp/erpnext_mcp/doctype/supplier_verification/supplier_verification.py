# SPDX-License-Identifier: MIT
"""Controller for Supplier Verification — supply chain verification records.

Each Supplier Verification record documents the verification of a supplier
within a Food Safety Plan's supply chain preventive control program. Tracks
audits, certificate reviews, testing results, and approval status. Warns when
a supplier's certificate has expired, because an expired certificate means the
supply chain control is no longer verified.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class SupplierVerification(Document):
	def validate(self):
		if not self.food_safety_plan:
			frappe.throw(_("Food Safety Plan is required."))
		if not self.supplier_name:
			frappe.throw(_("Supplier Name is required."))

		if self.certificate_expiry_date and str(self.certificate_expiry_date) < frappe.utils.today():
			frappe.msgprint(
				_("Certificate expired on {0} — supplier verification may need renewal.").format(
					self.certificate_expiry_date
				),
				title=_("Expired Certificate"),
				indicator="orange",
			)

# SPDX-License-Identifier: MIT
"""Controller for Pest Control Application — who applied what, where, how much. v0.204.0.

The non-crop sibling of Spray Application (docs/design/form_schema_and_labels.md
§5). Spray Application cannot hold rodent bait: it refuses a Housing Unit as a
block, divides by acres and opens REI/PHI windows. A pesticide application
record still has to say who applied which registered product, where, how much
and under which licence — so this is that record, written by the task that did
the work (`creates_record: Pest Control Application`) and read by the audit
packet's pest-control block.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class PestControlApplication(Document):
	def validate(self):
		if self.quantity is not None and float(self.quantity or 0) < 0:
			frappe.throw(_("Quantity cannot be negative."))
		if int(self.stations or 0) < 0:
			frappe.throw(_("Stations cannot be negative."))
		if self.location and not self.location_doctype:
			frappe.throw(_("Location {0} was given with no Location DocType.").format(self.location))
		if self.product and not self.epa_registration_number:
			from .... import compat

			self.epa_registration_number = (
				frappe.db.get_value("Item", self.product, "epa_registration_number")
				if compat.has_field("Item", "epa_registration_number")
				else ""
			) or ""

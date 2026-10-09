# SPDX-License-Identifier: MIT
from frappe.model.document import Document


class FarmPayrollEntry(Document):
	def on_update(self):
		# v0.293.0. Cancelling a run puts the bonuses and profit shares it paid back to Pending, so the next run pays
		# them. `pay_items.pending` also treats a cancelled run's items as payable, for a status written without a save.
		if self.get("status") == "Cancelled":
			from erpnext_mcp import pay_items

			pay_items.release(self.name)

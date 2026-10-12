# SPDX-License-Identifier: MIT
"""Controller for Field Pin. v0.286.0 — see erpnext_mcp/pins.py."""

from frappe.model.document import Document


class FieldPin(Document):
	def validate(self):
		# v0.323.0: who moved the pin and when.
		from erpnext_mcp import pin_moves

		pin_moves.stamp_pin(self)

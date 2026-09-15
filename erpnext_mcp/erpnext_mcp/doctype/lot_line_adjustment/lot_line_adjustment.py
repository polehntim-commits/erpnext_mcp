# SPDX-License-Identifier: MIT
"""Controller for Lot Line Adjustment — the agreement, and the Record Survey button.

v0.169.0. The rules live in `erpnext_mcp.land_adjustment` so that a Desk save,
an MCP tool and a test all ask the same function. This controller turns that
module's refusals into Desk messages and exposes the one action:
`record_survey`, called by the form's button, which runs the same code as the
`lla_record_survey` tool — including its System Manager check.

THE STANDALONE SUITE DOES NOT RUN APP CONTROLLERS; the test module calls these
methods on a constructed instance, which proves the logic and not the wiring.
"""

import frappe
from frappe.model.document import Document

from erpnext_mcp import land_adjustment
from erpnext_mcp.errors import ToolError


class LotLineAdjustment(Document):
	def validate(self):
		try:
			land_adjustment.validate_adjustment(self)
		except ToolError as exc:
			frappe.throw(str(exc), title="Lot Line Adjustment")

	def before_submit(self):
		missing = land_adjustment.submission_refusals(self)
		if missing:
			frappe.throw(
				f"This adjustment cannot go to the county yet: it has no {', '.join(missing)}.",
				title="Lot Line Adjustment",
			)
		if self.status in land_adjustment.DRAFT_STATUSES:
			self.status = land_adjustment.SUBMITTED

	def before_cancel(self):
		if self.get("status") == land_adjustment.RECORDED or self.get("survey_recorded_on"):
			frappe.throw(
				"This adjustment is recorded with the county, so it cannot be cancelled. Undoing it is a new adjustment.",
				title="Lot Line Adjustment",
			)
		self.status = land_adjustment.WITHDRAWN

	@frappe.whitelist()
	def record_survey(self):
		from erpnext_mcp.tools import land

		try:
			return land.lla_record_survey({"name": self.name}).data
		except ToolError as exc:
			frappe.throw(str(exc), title="Record Survey")

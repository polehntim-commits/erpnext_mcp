# SPDX-License-Identifier: MIT
"""Controller for Card Print Station. v0.208.0. docs/design/card_print_queue.md §2.3."""

import frappe
from frappe.model.document import Document

from erpnext_mcp import card_print


class CardPrintStation(Document):
	def validate(self):
		self.station_name = str(self.station_name or "").strip()
		types = card_print.lines(self.job_types)
		unknown = [t for t in types if t not in card_print.JOB_TYPES]
		if not types or unknown:
			frappe.throw(f"job_types is one per line from: {', '.join(card_print.JOB_TYPES)}.")
		self.job_types = "\n".join(types)
		self.companies = "\n".join(card_print.lines(self.companies))

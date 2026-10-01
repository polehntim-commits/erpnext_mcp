# SPDX-License-Identifier: MIT
"""Controller for Card Print Job. v0.208.0.

The docname is CPJ-<year>-<5 digits>. Every rule about who may ask, what is
rendered and how a job moves lives in `erpnext_mcp.card_print`; this only keeps
the row honest: a known status, 1–5 copies, and no way to edit a status from the
Desk form that the queue would not allow. docs/design/card_print_queue.md.
"""

import frappe
from frappe.model.document import Document

from erpnext_mcp import card_print, shifts

DOCTYPE = "Card Print Job"


class CardPrintJob(Document):
	def autoname(self):
		year = str(frappe.utils.today())[:4]
		self.name = shifts.next_in_series(DOCTYPE, "CPJ", year, width=5)

	def validate(self):
		if self.status not in card_print.STATUSES:
			frappe.throw(f"status must be one of {', '.join(card_print.STATUSES)}.")
		if self.job_type not in card_print.JOB_TYPES:
			frappe.throw(f"job_type must be one of {', '.join(card_print.JOB_TYPES)}.")
		copies = int(self.copies or 1)
		if not 1 <= copies <= card_print.MAX_COPIES:
			frappe.throw(f"copies must be 1 to {card_print.MAX_COPIES}.")
		self.copies = copies
		if self.sides not in card_print.SIDES:
			frappe.throw("sides is Single or Dual.")
		if self.name and frappe.db.exists(DOCTYPE, self.name):
			before = frappe.db.get_value(DOCTYPE, self.name, "status")
			if before != self.status and not self.flags.get("queue_move"):
				frappe.throw(
					"a print job's status changes through the queue (retry, cancel), not by editing it."
				)

	@frappe.whitelist()
	def retry(self):
		return card_print.desk(card_print.retry, self.name)

	@frappe.whitelist()
	def cancel_job(self):
		return card_print.desk(card_print.cancel, self.name)

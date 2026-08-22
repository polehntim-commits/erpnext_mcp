# SPDX-License-Identifier: MIT
"""Controller for Satellite Backfill Cursor — bookkeeping about downloads, not about crops.

WHAT THIS RECORD IS WORTH. Nothing on it is a measurement. It says how far back
and how far forward imagery has already been pulled for one block and one index,
and its entire value is that losing it costs money: a backfill with no cursor
starts at the beginning and pays the provider again for twelve months somebody
has already bought. That is why it is worth migrating out of the sidecar even
though the farm_app's own copy of it is empty — the cost of losing it is not
visible in the record, it is visible on an invoice.

ONE CURSOR PER BLOCK PER INDEX, enforced here. A backfill of NDVI says nothing
about how far back moisture has been fetched: they are separate requests, they
are billed separately, and a single cursor covering both would let a completed
NDVI walk suppress a moisture walk that never happened.

THE WINDOW IS HALF-OPEN ON BOTH SIDES AND THE FIELD NAMES SAY SO. `oldest_fetched`
and `newest_fetched` are passes that HAVE been pulled, so a backfill resumes
strictly before the first and a forward sweep strictly after the second. Reading
them as bounds to resume AT re-fetches one pass every run — cheap, invisible, and
exactly the kind of thing that is discovered a year later on a bill.

AN INVERTED WINDOW IS REFUSED. `oldest_fetched` after `newest_fetched` describes
a range that cannot exist, and a scheduler reading it would either fetch nothing
for ever or fetch everything every night. Neither failure announces itself.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class SatelliteBackfillCursor(Document):
	def validate(self):
		if not self.field:
			frappe.throw(_("Block is required — a cursor with no block records nothing."))

		self._copy_from_block()
		self._check_window()
		self._check_duplicate()

	def _copy_from_block(self) -> None:
		if not self.company:
			self.company = frappe.db.get_value("Field", self.field, "owning_entity")

	def _check_window(self) -> None:
		if not (self.oldest_fetched and self.newest_fetched):
			return
		if str(self.oldest_fetched) > str(self.newest_fetched):
			frappe.throw(
				_(
					"Oldest Fetched ({0}) is after Newest Fetched ({1}). That window cannot exist, "
					"and a scheduler reading it fetches either nothing for ever or everything "
					"every night — neither of which announces itself."
				).format(self.oldest_fetched, self.newest_fetched),
				title=_("Inverted Window"),
			)

	def _check_duplicate(self) -> None:
		duplicate = frappe.db.get_value(
			"Satellite Backfill Cursor",
			{
				"field": self.field,
				"metric_type": self.metric_type,
				"name": ("!=", self.name or ""),
			},
			"name",
		)
		if duplicate:
			frappe.throw(
				_(
					"{0} is already the {1} cursor for this block. Two cursors for one block and "
					"one index disagree the moment either is updated, and whichever a scheduler "
					"reads first decides what gets re-downloaded."
				).format(duplicate, self.metric_type),
				title=_("Cursor Already Exists"),
			)
